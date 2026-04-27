-- @bug-id: B-11
-- @sev: P2
-- @phase: 6
-- @slice: contracts
-- @deterministic: must FAIL on baseline (inline subquery), PASS post-011
--
-- B-11: admin_count_profiles and admin_list_profiles MUST call
-- public.is_admin() in their function body (not an inline role subquery).
-- This test inspects pg_proc.prosrc via pg_get_functiondef().

DO $$
DECLARE
  fn   text;
  def  text;
  bad  text := '';
  fns  text[] := ARRAY['admin_count_profiles', 'admin_list_profiles'];
BEGIN
  FOREACH fn IN ARRAY fns LOOP
    SELECT pg_get_functiondef(p.oid) INTO def
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname = fn
     LIMIT 1;

    IF def IS NULL THEN
      bad := bad || format(E'  %s: function not found\n', fn);
      CONTINUE;
    END IF;

    -- Must call public.is_admin()
    IF def NOT ILIKE '%is_admin%' THEN
      bad := bad || format(
        E'  %s: function body does not call is_admin() — still uses inline subquery\n', fn
      );
    END IF;

    -- Must NOT use the old inline pattern: SELECT role FROM ... WHERE id = auth.uid()
    IF def ~* 'SELECT\s+role\s+FROM\s+.*profiles.*WHERE\s+id\s*=\s*auth\.uid\(\)' THEN
      bad := bad || format(
        E'  %s: function body still contains inline role-subquery (not is_admin)\n', fn
      );
    END IF;

    -- Must have search_path set
    IF NOT EXISTS (
      SELECT 1 FROM pg_proc p2
        JOIN pg_namespace n2 ON n2.oid = p2.pronamespace
       WHERE n2.nspname = 'public' AND p2.proname = fn
         AND EXISTS (
           SELECT 1 FROM unnest(COALESCE(p2.proconfig, ARRAY[]::text[])) e
            WHERE e ILIKE 'search_path=%'
         )
    ) THEN
      bad := bad || format(E'  %s: missing SET search_path in proconfig\n', fn);
    END IF;
  END LOOP;

  IF length(bad) > 0 THEN
    RAISE EXCEPTION E'C10 FAIL: admin helpers still use inline check:\n%', bad;
  END IF;
END $$;

SELECT 'C10 PASS: admin_count_profiles and admin_list_profiles use public.is_admin()' AS result;
