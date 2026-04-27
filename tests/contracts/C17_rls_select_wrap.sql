-- C17_rls_select_wrap.sql
-- Contract test: B-33 — All RLS policies must use (SELECT auth.uid()) not bare auth.uid()
-- Bare auth.uid() in USING/WITH CHECK is re-evaluated per row (InitPlan overhead).
-- Wrapping in (SELECT ...) makes the planner cache it once per query.
--
-- Note: PostgreSQL normalizes "(SELECT auth.uid())" to
-- "( SELECT auth.uid() AS uid)" in pg_policies.qual/with_check.
-- We detect bare calls by checking that auth.uid() is NOT immediately
-- preceded by "SELECT " (case-insensitive) in the stored policy text.

-- Test 1: No policy USING clause should contain bare auth.uid()
-- (i.e., auth.uid() not immediately preceded by 'SELECT ')
DO $$
DECLARE
  v_bad_policy record;
  v_found boolean := false;
BEGIN
  FOR v_bad_policy IN
    SELECT tablename, policyname, qual
    FROM pg_policies
    WHERE qual IS NOT NULL
      -- Regex: auth.uid() NOT preceded by 'select ' (case-insensitive)
      AND qual ~ '(?<![Ss][Ee][Ll][Ee][Cc][Tt] )auth\.uid\(\)'
  LOOP
    RAISE WARNING 'B-33: bare auth.uid() in USING of policy %.%: %',
      v_bad_policy.tablename, v_bad_policy.policyname, v_bad_policy.qual;
    v_found := true;
  END LOOP;
  IF v_found THEN
    RAISE EXCEPTION 'B-33 FAIL: one or more RLS USING clauses contain bare auth.uid()';
  END IF;
END $$;

-- Test 2: No policy WITH CHECK should contain bare auth.uid()
DO $$
DECLARE
  v_bad_policy record;
  v_found boolean := false;
BEGIN
  FOR v_bad_policy IN
    SELECT tablename, policyname, with_check
    FROM pg_policies
    WHERE with_check IS NOT NULL
      AND with_check ~ '(?<![Ss][Ee][Ll][Ee][Cc][Tt] )auth\.uid\(\)'
  LOOP
    RAISE WARNING 'B-33: bare auth.uid() in WITH CHECK of policy %.%: %',
      v_bad_policy.tablename, v_bad_policy.policyname, LEFT(v_bad_policy.with_check, 200);
    v_found := true;
  END LOOP;
  IF v_found THEN
    RAISE EXCEPTION 'B-33 FAIL: one or more RLS WITH CHECK clauses contain bare auth.uid()';
  END IF;
END $$;

-- Test 3: No policy should contain bare auth.role()
DO $$
DECLARE
  v_bad_policy record;
  v_found boolean := false;
BEGIN
  FOR v_bad_policy IN
    SELECT tablename, policyname
    FROM pg_policies
    WHERE (
      (qual IS NOT NULL AND qual ~ '(?<![Ss][Ee][Ll][Ee][Cc][Tt] )auth\.role\(\)')
      OR
      (with_check IS NOT NULL AND with_check ~ '(?<![Ss][Ee][Ll][Ee][Cc][Tt] )auth\.role\(\)')
    )
  LOOP
    RAISE WARNING 'B-33: bare auth.role() in policy %.%',
      v_bad_policy.tablename, v_bad_policy.policyname;
    v_found := true;
  END LOOP;
  IF v_found THEN
    RAISE EXCEPTION 'B-33 FAIL: one or more RLS policies contain bare auth.role()';
  END IF;
END $$;

-- Test 4: Specifically verify credit_clawbacks_owner_select uses SELECT-wrapped uid
DO $$
DECLARE
  v_qual text;
BEGIN
  SELECT qual INTO v_qual
  FROM pg_policies
  WHERE tablename = 'credit_clawbacks' AND policyname = 'credit_clawbacks_owner_select';
  
  IF v_qual IS NULL THEN
    RAISE EXCEPTION 'B-33 FAIL: credit_clawbacks_owner_select policy not found';
  END IF;
  
  -- Must contain SELECT auth.uid() (wrapped form)
  IF NOT (v_qual ~ 'SELECT auth\.uid\(\)') THEN
    RAISE EXCEPTION 'B-33 FAIL: credit_clawbacks_owner_select USING not wrapped with SELECT: %', v_qual;
  END IF;
  
  -- Must NOT contain bare auth.uid()
  IF v_qual ~ '(?<![Ss][Ee][Ll][Ee][Cc][Tt] )auth\.uid\(\)' THEN
    RAISE EXCEPTION 'B-33 FAIL: credit_clawbacks_owner_select USING has bare auth.uid(): %', v_qual;
  END IF;
END $$;

-- Test 5: Verify profiles_owner_update_safe USING clause is SELECT-wrapped
DO $$
DECLARE
  v_qual text;
BEGIN
  SELECT qual INTO v_qual
  FROM pg_policies
  WHERE tablename = 'profiles' AND policyname = 'profiles_owner_update_safe';
  
  IF v_qual IS NULL THEN
    RAISE EXCEPTION 'B-33 FAIL: profiles_owner_update_safe policy not found';
  END IF;
  
  IF NOT (v_qual ~ 'SELECT auth\.uid\(\)') THEN
    RAISE EXCEPTION 'B-33 FAIL: profiles_owner_update_safe USING not SELECT-wrapped: %', v_qual;
  END IF;
  
  IF v_qual ~ '(?<![Ss][Ee][Ll][Ee][Cc][Tt] )auth\.uid\(\)' THEN
    RAISE EXCEPTION 'B-33 FAIL: profiles_owner_update_safe USING has bare auth.uid(): %', v_qual;
  END IF;
END $$;
