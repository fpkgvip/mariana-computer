-- @bug-id: R5 / signature preservation
-- @sev: critical
-- @phase: 0
-- @slice: contracts
-- @deterministic: must PASS on baseline AND post-004 (regression guard)
--
-- This is a "must remain green" assertion: api.py at line 6206 calls
-- admin_set_credits(target_user_id, new_credits, is_delta) with types
-- (uuid, integer, boolean). We must NEVER ship a migration that breaks
-- this signature. This test runs on both baseline and post-004 and must
-- pass on both.

DO $$
DECLARE
  sig text;
  ret text;
BEGIN
  SELECT oidvectortypes(p.proargtypes), pg_get_function_result(p.oid) INTO sig, ret
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname='public' AND p.proname='admin_set_credits' LIMIT 1;

  IF sig IS DISTINCT FROM 'uuid, integer, boolean' THEN
    RAISE EXCEPTION 'C07 FAIL: admin_set_credits args = %, expected (uuid, integer, boolean)', sig;
  END IF;
  IF ret IS DISTINCT FROM 'integer' THEN
    RAISE EXCEPTION 'C07 FAIL: admin_set_credits returns %, expected integer', ret;
  END IF;
END $$;

SELECT 'C07 PASS: admin_set_credits signature preserved' AS result;
