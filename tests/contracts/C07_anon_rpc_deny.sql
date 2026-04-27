-- @bug-id: B-01
-- @sev: P0
-- @phase: 6
-- @slice: contracts
-- @deterministic: must FAIL on baseline (anon/authenticated have EXECUTE),
--                 must PASS post-005 (partial-revoke posture — see below).
--
-- B-01: anon-callable SECURITY DEFINER RPCs are the most severe finding of
-- Loop 6. Live evidence (pg_proc.proacl) shows EXECUTE granted to PUBLIC,
-- anon, authenticated, postgres, service_role on every credit / profile /
-- admin RPC. A logged-out attacker can:
--   - call get_user_tokens(any_uuid)              -> IDOR balance read
--   - call check_balance(any_uuid)                -> IDOR balance read
--   - call get_stripe_customer_id(any_uuid)       -> IDOR Stripe id leak
--   - call add_credits(any_uuid, N)               -> mint credits
--   - call deduct_credits(any_uuid, N)            -> drain credits
--   - call update_profile_by_id(any_uuid, jsonb)  -> override billing fields
--   - call update_profile_by_stripe_customer(...)
--
-- Migration 005 applies a split posture:
--   Group A (backend-only)  — revoke from PUBLIC, anon, authenticated.
--   Group B (admin-gated)   — revoke from PUBLIC, anon; keep authenticated
--                              (inline is_admin() check + api.py forwards JWT).
--
-- This contract asserts both tracks and also asserts the inline is_admin()
-- gate is present in every Group B function body (defense in depth).

DO $$
DECLARE
  fn   text;
  rl   text;
  bad  text := '';
  group_a text[] := ARRAY[
    'add_credits',
    'deduct_credits',
    'get_user_tokens',
    'check_balance',
    'get_stripe_customer_id',
    'update_profile_by_id',
    'update_profile_by_stripe_customer',
    'expire_credits',
    'spend_credits',
    'grant_credits',
    'refund_credits',
    'handle_new_user',
    'admin_audit_insert'  -- B-12: service_role only
  ];
  -- B-12 fix: admin_audit_insert moved to group_a (service_role only)
  group_b text[] := ARRAY[
    'admin_set_credits',
    'admin_adjust_credits',
    'admin_count_profiles',
    'admin_list_profiles'
  ];
  service_backend text[] := ARRAY[
    'add_credits','deduct_credits','get_user_tokens','check_balance',
    'get_stripe_customer_id','update_profile_by_id',
    'update_profile_by_stripe_customer','admin_set_credits',
    'admin_adjust_credits','admin_audit_insert','admin_count_profiles',
    'admin_list_profiles','expire_credits','spend_credits',
    'grant_credits','refund_credits'
  ];
  group_a_hostile_roles text[] := ARRAY['public','anon','authenticated'];
  group_b_hostile_roles text[] := ARRAY['public','anon'];
BEGIN
  -- ---------------- Group A: fully revoked from hostile roles ----------------
  FOREACH fn IN ARRAY group_a LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname='public' AND p.proname = fn
    ) THEN
      CONTINUE;
    END IF;

    FOREACH rl IN ARRAY group_a_hostile_roles LOOP
      IF EXISTS (
        SELECT 1 FROM information_schema.routine_privileges
        WHERE specific_schema='public'
          AND routine_name = fn
          AND grantee = CASE WHEN rl='public' THEN 'PUBLIC' ELSE rl END
          AND privilege_type = 'EXECUTE'
      ) THEN
        bad := bad || format(E'  [A] %s: role %s has EXECUTE (backend-only fn must be service_role-gated)\n', fn, rl);
      END IF;
    END LOOP;
  END LOOP;

  -- ---------------- Group B: revoked from PUBLIC + anon only ----------------
  FOREACH fn IN ARRAY group_b LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname='public' AND p.proname = fn
    ) THEN
      CONTINUE;
    END IF;

    FOREACH rl IN ARRAY group_b_hostile_roles LOOP
      IF EXISTS (
        SELECT 1 FROM information_schema.routine_privileges
        WHERE specific_schema='public'
          AND routine_name = fn
          AND grantee = CASE WHEN rl='public' THEN 'PUBLIC' ELSE rl END
          AND privilege_type = 'EXECUTE'
      ) THEN
        bad := bad || format(E'  [B] %s: role %s has EXECUTE (admin-gated RPC must deny unauthenticated)\n', fn, rl);
      END IF;
    END LOOP;

    -- Group B: authenticated MUST retain EXECUTE (api.py admin forwards JWT).
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.routine_privileges
      WHERE specific_schema='public'
        AND routine_name = fn
        AND grantee = 'authenticated'
        AND privilege_type = 'EXECUTE'
    ) THEN
      bad := bad || format(E'  [B] %s: authenticated MISSING EXECUTE (admin endpoint will 403)\n', fn);
    END IF;

    -- Group B: inline admin gate MUST be present (defense in depth).
    -- Either a call to public.is_admin(...) OR an inline `role='admin'`
    -- subselect on profiles is acceptable.
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname='public' AND p.proname = fn
        AND (
          pg_get_functiondef(p.oid) ILIKE '%is_admin%'
          OR pg_get_functiondef(p.oid) ~* $admin$=\s*'admin'$admin$
        )
    ) THEN
      bad := bad || format(E'  [B] %s: function body lacks an admin gate (no is_admin() call and no =''admin'' comparison)\n', fn);
    END IF;
  END LOOP;

  -- ---------------- service_role must keep EXECUTE on backend-invoked RPCs --
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    FOREACH fn IN ARRAY service_backend LOOP
      IF NOT EXISTS (
        SELECT 1 FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname='public' AND p.proname = fn
      ) THEN
        CONTINUE;
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM information_schema.routine_privileges
        WHERE specific_schema='public'
          AND routine_name = fn
          AND grantee = 'service_role'
          AND privilege_type = 'EXECUTE'
      ) THEN
        bad := bad || format(E'  [S] %s: service_role MISSING EXECUTE (backend will break)\n', fn);
      END IF;
    END LOOP;
  END IF;

  IF length(bad) > 0 THEN
    RAISE EXCEPTION E'C07 FAIL: dangerous RPC EXECUTE posture:\n%', bad;
  END IF;
END $$;

SELECT 'C07 PASS: B-01 partial-revoke posture holds (group A backend-only, group B admin-gated)' AS result;
