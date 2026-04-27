-- @bug-id: B-13
-- @sev: P2
-- @phase: 6
-- @slice: contracts
-- @deterministic: must FAIL on baseline (anon/authenticated have EXECUTE),
--                 must PASS post-011 (service_role only).
--
-- B-13: expire_credits MUST NOT be executable by anon or authenticated.
-- Only service_role (and postgres) should have EXECUTE.

DO $$
DECLARE
  bad  text := '';
  rl   text;
  deny_roles text[] := ARRAY['anon', 'authenticated'];
BEGIN
  FOREACH rl IN ARRAY deny_roles LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.routine_privileges
       WHERE specific_schema = 'public'
         AND routine_name = 'expire_credits'
         AND grantee = rl
         AND privilege_type = 'EXECUTE'
    ) THEN
      bad := bad || format(
        E'  expire_credits: role %s has EXECUTE (must be service_role only)\n', rl
      );
    END IF;
  END LOOP;

  -- service_role must retain EXECUTE for cron path
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.routine_privileges
     WHERE specific_schema = 'public'
       AND routine_name = 'expire_credits'
       AND grantee = 'service_role'
       AND privilege_type = 'EXECUTE'
  ) THEN
    bad := bad || E'  expire_credits: service_role MISSING EXECUTE (cron path will break)\n';
  END IF;

  -- Function must exist
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname = 'expire_credits'
  ) THEN
    bad := bad || E'  expire_credits: function not found in public schema\n';
  END IF;

  IF length(bad) > 0 THEN
    RAISE EXCEPTION E'C12 FAIL: expire_credits EXECUTE posture violation:\n%', bad;
  END IF;
END $$;

SELECT 'C12 PASS: expire_credits is service_role only (anon+authenticated denied)' AS result;
