-- 005_loop6_b01_revoke_anon_rpcs.sql
--
-- Loop 6 / B-01 (P0): REVOKE EXECUTE on dangerous SECURITY DEFINER RPCs
-- from PUBLIC / anon (and, for backend-only RPCs, also from authenticated).
--
-- ----------------------------------------------------------------------------
-- Why the split?
-- ----------------------------------------------------------------------------
--
-- Two groups of SECURITY DEFINER RPCs exist on NestD live:
--
--   GROUP A — backend-only (no legitimate authenticated caller path).
--   These are invoked exclusively by the FastAPI backend using
--   SUPABASE_SERVICE_KEY (see mariana/billing/ledger.py and the Stripe
--   webhook path). An authenticated user has no legitimate reason to call
--   them, so we revoke from PUBLIC, anon, AND authenticated, leaving only
--   service_role (and postgres as owner).
--
--     add_credits(uuid, integer)
--     deduct_credits(uuid, integer)
--     get_user_tokens(uuid)
--     check_balance(uuid)
--     get_stripe_customer_id(uuid)
--     update_profile_by_id(uuid, jsonb)
--     update_profile_by_stripe_customer(text, jsonb)
--     expire_credits()
--     spend_credits(uuid, integer, text, text, jsonb)
--     grant_credits(uuid, integer, text, text, text, timestamptz)
--     refund_credits(uuid, integer, text, text)
--     handle_new_user()
--
--   GROUP B — admin-gated (inline is_admin() check inside the function).
--   These ARE called by the FastAPI admin endpoints with the caller's user
--   JWT forwarded in Authorization, so the PostgREST role resolved from
--   the JWT is 'authenticated'. We revoke from PUBLIC and anon (so an
--   unauthenticated attacker can not call them even if they get past the
--   API gateway), but keep authenticated EXECUTE. The function body
--   immediately raises 'not_admin' if public.is_admin(auth.uid()) is
--   false, so a non-admin 'authenticated' caller gains nothing.
--
--     admin_set_credits(uuid, integer, boolean)       -- uses auth.uid()
--     admin_adjust_credits(uuid, uuid, text, integer, text)  -- p_caller + is_admin(p_caller)
--     admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text)
--                                                      -- p_actor_id + is_admin(p_actor_id)
--     admin_count_profiles()                           -- uses auth.uid()
--     admin_list_profiles()                            -- uses auth.uid()
--
-- Defense-in-depth: we ALSO assert every Group B RPC calls public.is_admin()
-- in its body (see invariants block). If a future migration removes the
-- inline gate, this migration will fail and must be re-worked.
--
-- This migration is idempotent. It is safe to re-apply.

BEGIN;

-- ---------------------------------------------------------------------------
-- Pre-flight sanity
-- ---------------------------------------------------------------------------

DO $preflight$
DECLARE
  group_a_offenders int;
  group_b_offenders int;
BEGIN
  SELECT count(*) INTO group_a_offenders
  FROM information_schema.routine_privileges
  WHERE specific_schema='public'
    AND grantee IN ('PUBLIC','anon','authenticated')
    AND privilege_type='EXECUTE'
    AND routine_name IN (
      'add_credits','deduct_credits','get_user_tokens','check_balance',
      'get_stripe_customer_id','update_profile_by_id',
      'update_profile_by_stripe_customer','expire_credits','spend_credits',
      'grant_credits','refund_credits','handle_new_user'
    );

  SELECT count(*) INTO group_b_offenders
  FROM information_schema.routine_privileges
  WHERE specific_schema='public'
    AND grantee IN ('PUBLIC','anon')
    AND privilege_type='EXECUTE'
    AND routine_name IN (
      'admin_set_credits','admin_adjust_credits','admin_audit_insert',
      'admin_count_profiles','admin_list_profiles'
    );

  RAISE NOTICE '005 pre-flight: group_a_offenders=%, group_b_offenders=% (0 is fine on idempotent re-apply)',
    group_a_offenders, group_b_offenders;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- GROUP A: backend-only RPCs — fully revoke from PUBLIC, anon, authenticated
-- ---------------------------------------------------------------------------

REVOKE EXECUTE ON FUNCTION public.add_credits(uuid, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.deduct_credits(uuid, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_user_tokens(uuid)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.check_balance(uuid)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_stripe_customer_id(uuid)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.update_profile_by_id(uuid, jsonb)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.update_profile_by_stripe_customer(text, jsonb)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.expire_credits()
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.handle_new_user()
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- GROUP B: admin-gated RPCs — revoke from PUBLIC + anon only. Keep
-- authenticated EXECUTE (api.py admin endpoints forward the caller's JWT).
-- ---------------------------------------------------------------------------

REVOKE EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean)
  FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.admin_adjust_credits(uuid, uuid, text, integer, text)
  FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text)
  FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.admin_count_profiles()
  FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.admin_list_profiles()
  FROM PUBLIC, anon;

-- ---------------------------------------------------------------------------
-- Explicit GRANT to service_role (backend key role).
-- postgres already has EXECUTE as owner.
-- For Group B, also explicitly re-grant EXECUTE to authenticated to be
-- unambiguous (idempotent re-apply of earlier migrations could have revoked).
-- ---------------------------------------------------------------------------

-- Group A: service_role only
GRANT EXECUTE ON FUNCTION public.add_credits(uuid, integer)                    TO service_role;
GRANT EXECUTE ON FUNCTION public.deduct_credits(uuid, integer)                 TO service_role;
GRANT EXECUTE ON FUNCTION public.get_user_tokens(uuid)                         TO service_role;
GRANT EXECUTE ON FUNCTION public.check_balance(uuid)                           TO service_role;
GRANT EXECUTE ON FUNCTION public.get_stripe_customer_id(uuid)                  TO service_role;
GRANT EXECUTE ON FUNCTION public.update_profile_by_id(uuid, jsonb)             TO service_role;
GRANT EXECUTE ON FUNCTION public.update_profile_by_stripe_customer(text, jsonb)TO service_role;
GRANT EXECUTE ON FUNCTION public.expire_credits()                              TO service_role;
GRANT EXECUTE ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz) TO service_role;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text)     TO service_role;
-- handle_new_user: trigger fn, no user-facing grant needed.

-- Group B: service_role AND authenticated
GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean)     TO service_role, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_adjust_credits(uuid, uuid, text, integer, text) TO service_role, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text) TO service_role, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_count_profiles()                        TO service_role, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_list_profiles()                         TO service_role, authenticated;

-- ---------------------------------------------------------------------------
-- Post-fix invariants
-- ---------------------------------------------------------------------------
DO $post$
DECLARE
  group_a_hostile int;
  group_b_anon int;
  group_b_missing_auth int;
  missing_service int;
  missing_is_admin_gate text;
BEGIN
  -- Group A: NO hostile role may hold EXECUTE.
  SELECT count(*) INTO group_a_hostile
  FROM information_schema.routine_privileges
  WHERE specific_schema='public'
    AND grantee IN ('PUBLIC','anon','authenticated')
    AND privilege_type='EXECUTE'
    AND routine_name IN (
      'add_credits','deduct_credits','get_user_tokens','check_balance',
      'get_stripe_customer_id','update_profile_by_id',
      'update_profile_by_stripe_customer','expire_credits','spend_credits',
      'grant_credits','refund_credits','handle_new_user'
    );
  IF group_a_hostile > 0 THEN
    RAISE EXCEPTION '005 FAIL: % hostile grants remain on Group A (backend-only) RPCs', group_a_hostile;
  END IF;

  -- Group B: PUBLIC/anon must be stripped; authenticated MUST retain EXECUTE.
  SELECT count(*) INTO group_b_anon
  FROM information_schema.routine_privileges
  WHERE specific_schema='public'
    AND grantee IN ('PUBLIC','anon')
    AND privilege_type='EXECUTE'
    AND routine_name IN (
      'admin_set_credits','admin_adjust_credits','admin_audit_insert',
      'admin_count_profiles','admin_list_profiles'
    );
  IF group_b_anon > 0 THEN
    RAISE EXCEPTION '005 FAIL: % unauthenticated grants remain on Group B (admin-gated) RPCs', group_b_anon;
  END IF;

  SELECT count(*) INTO group_b_missing_auth
  FROM unnest(ARRAY[
    'admin_set_credits','admin_adjust_credits','admin_audit_insert',
    'admin_count_profiles','admin_list_profiles'
  ]) AS fn
  WHERE NOT EXISTS (
    SELECT 1 FROM information_schema.routine_privileges
    WHERE specific_schema='public'
      AND routine_name = fn
      AND grantee = 'authenticated'
      AND privilege_type = 'EXECUTE'
  );
  IF group_b_missing_auth > 0 THEN
    RAISE EXCEPTION '005 FAIL: % Group B RPCs are missing authenticated EXECUTE (api.py will 500)', group_b_missing_auth;
  END IF;

  -- service_role must have EXECUTE on all backend-invoked RPCs.
  SELECT count(*) INTO missing_service
  FROM unnest(ARRAY[
    'add_credits','deduct_credits','get_user_tokens','check_balance',
    'get_stripe_customer_id','update_profile_by_id',
    'update_profile_by_stripe_customer','admin_set_credits',
    'admin_adjust_credits','admin_audit_insert','admin_count_profiles',
    'admin_list_profiles','expire_credits','spend_credits',
    'grant_credits','refund_credits'
  ]) AS fn
  WHERE NOT EXISTS (
    SELECT 1 FROM information_schema.routine_privileges
    WHERE specific_schema='public'
      AND routine_name = fn
      AND grantee = 'service_role'
      AND privilege_type = 'EXECUTE'
  );
  IF missing_service > 0 THEN
    RAISE EXCEPTION '005 FAIL: service_role missing EXECUTE on % backend-facing fns', missing_service;
  END IF;

  -- Defense-in-depth: every Group B RPC MUST gate against admin.
  -- Two acceptable shapes live in production today:
  --   (a) explicit call to public.is_admin(...)            [preferred]
  --   (b) inline subselect: role = 'admin' on profiles     [legacy]
  -- If a future change strips BOTH shapes while authenticated retains
  -- EXECUTE, this migration must be re-worked before re-apply.
  SELECT string_agg(fn, ', ') INTO missing_is_admin_gate
  FROM unnest(ARRAY[
    'admin_set_credits','admin_adjust_credits','admin_audit_insert',
    'admin_count_profiles','admin_list_profiles'
  ]) AS fn
  WHERE NOT EXISTS (
    SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname='public' AND p.proname = fn
      AND (
        pg_get_functiondef(p.oid) ILIKE '%is_admin%'
        OR pg_get_functiondef(p.oid) ~* $admin$=\s*'admin'$admin$
      )
  );
  IF missing_is_admin_gate IS NOT NULL THEN
    RAISE EXCEPTION '005 FAIL: Group B RPC(s) lack is_admin() inline gate: %', missing_is_admin_gate;
  END IF;

  RAISE NOTICE '005 invariants: group_a_hostile=0 group_b_anon=0 group_b_auth=complete service_role=complete is_admin_gate=complete';
END
$post$;

COMMIT;
