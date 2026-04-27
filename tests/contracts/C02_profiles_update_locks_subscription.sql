-- @bug-id: R1
-- @sev: high
-- @phase: 0
-- @slice: contracts
-- @deterministic: must FAIL on baseline, PASS post-004
--
-- R1 functional: a user CANNOT modify their own subscription_status or
-- subscription_plan via direct UPDATE (the privilege escalation surface).
--
-- This test sets up a user, switches role to authenticated, sets
-- request.jwt.claim.sub, and tries to write subscription_status. On baseline
-- this should SUCCEED (bug). Post-004 it must FAIL (fixed).

BEGIN;

-- Seed
INSERT INTO public.profiles (id, email, role, plan, subscription_status, subscription_plan, tokens)
VALUES (
  '11111111-1111-1111-1111-111111111111'::uuid,
  'r1user@test.local',
  'user',
  'flagship',
  'none',
  'none',
  500
);

-- Simulate Supabase: switch to authenticated role and set JWT claim.
SET LOCAL ROLE authenticated;
SET LOCAL request.jwt.claim.sub = '11111111-1111-1111-1111-111111111111';

-- Try the privilege escalation: set subscription_status to 'active'.
-- Capture rowcount through diagnostics.
DO $$
DECLARE
  n int;
BEGIN
  UPDATE public.profiles
     SET subscription_status = 'active'
   WHERE id = '11111111-1111-1111-1111-111111111111'::uuid;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN
    RAISE EXCEPTION 'C02 FAIL: user successfully wrote subscription_status (% rows). Privilege escalation surface present.', n;
  END IF;
EXCEPTION WHEN insufficient_privilege OR check_violation THEN
  -- Expected post-fix: RLS blocks the write. Treat as pass.
  RAISE NOTICE 'C02 PASS via exception: %', SQLERRM;
END $$;

ROLLBACK;

SELECT 'C02 PASS: subscription_status write blocked' AS result;
