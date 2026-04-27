-- @bug-id: R1
-- @sev: high
-- @phase: 0
-- @slice: contracts
-- @deterministic: must FAIL on baseline, PASS post-004
--
-- R1: profiles must have exactly ONE PERMISSIVE UPDATE policy.
-- The looser "Users can update own profile" must be dropped because it
-- omits subscription_status / subscription_plan / subscription_current_period_end /
-- suspended_reason / admin_notes from its WITH CHECK clause, allowing
-- privilege escalation when OR'd with profiles_owner_update_safe.

DO $$
DECLARE
  n int;
BEGIN
  SELECT count(*) INTO n
  FROM pg_policy p
  JOIN pg_class c ON c.oid = p.polrelid
  JOIN pg_namespace nm ON nm.oid = c.relnamespace
  WHERE nm.nspname='public' AND c.relname='profiles' AND p.polcmd='w';

  IF n <> 1 THEN
    RAISE EXCEPTION 'C01 FAIL: profiles UPDATE policy count = %, expected 1', n;
  END IF;
END $$;

SELECT 'C01 PASS: profiles has exactly 1 UPDATE policy' AS result;
