-- @bug-id: R5+search_path
-- @sev: medium
-- @phase: 0
-- @slice: contracts
-- @deterministic: must FAIL on baseline, PASS post-004
--
-- Every SECURITY DEFINER function must declare SET search_path = '' (or
-- equivalently restrict to known schemas). admin_set_credits on baseline
-- has NO search_path config — search_path injection risk.

DO $$
DECLARE
  cfg text[];
  has_set boolean;
BEGIN
  SELECT proconfig INTO cfg FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname='public' AND p.proname='admin_set_credits' LIMIT 1;

  -- pg stores SET search_path = '' as 'search_path=""' inside proconfig.
  has_set := EXISTS (SELECT 1 FROM unnest(COALESCE(cfg, ARRAY[]::text[])) e WHERE e ILIKE 'search_path=%');

  IF NOT has_set THEN
    RAISE EXCEPTION 'C05 FAIL: admin_set_credits missing SET search_path (proconfig: %)', cfg;
  END IF;
END $$;

SELECT 'C05 PASS: admin_set_credits has SET search_path = ''''' AS result;
