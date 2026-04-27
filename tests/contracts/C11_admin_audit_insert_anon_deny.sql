-- @bug-id: B-12
-- @sev: P2
-- @phase: 6
-- @slice: contracts
-- @deterministic: must FAIL on baseline (anon/authenticated have EXECUTE),
--                 must PASS post-011 (service_role only).
--
-- B-12: admin_audit_insert MUST NOT be executable by anon or authenticated.
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
         AND routine_name = 'admin_audit_insert'
         AND grantee = rl
         AND privilege_type = 'EXECUTE'
    ) THEN
      bad := bad || format(
        E'  admin_audit_insert: role %s has EXECUTE (must be service_role only)\n', rl
      );
    END IF;
  END LOOP;

  -- service_role must still have EXECUTE
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.routine_privileges
     WHERE specific_schema = 'public'
       AND routine_name = 'admin_audit_insert'
       AND grantee = 'service_role'
       AND privilege_type = 'EXECUTE'
  ) THEN
    bad := bad || E'  admin_audit_insert: service_role MISSING EXECUTE (will break backend)\n';
  END IF;

  -- Function must exist
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname = 'admin_audit_insert'
  ) THEN
    bad := bad || E'  admin_audit_insert: function not found in public schema\n';
  END IF;

  IF length(bad) > 0 THEN
    RAISE EXCEPTION E'C11 FAIL: admin_audit_insert EXECUTE posture violation:\n%', bad;
  END IF;
END $$;

SELECT 'C11 PASS: admin_audit_insert is service_role only (anon+authenticated denied)' AS result;
