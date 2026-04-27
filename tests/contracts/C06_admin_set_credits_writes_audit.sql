-- @bug-id: R5
-- @sev: medium
-- @phase: 0
-- @slice: contracts
-- @deterministic: must FAIL on baseline, PASS post-004
--
-- R5: admin_set_credits MUST write to audit_log on every successful update.
-- Baseline: no audit row written. Post-004: one row with action='admin.set_credits'.

BEGIN;

-- Seed admin and target.
INSERT INTO public.profiles (id, email, role, tokens) VALUES
  ('44444444-4444-4444-4444-444444444444'::uuid, 'c06admin@test.local', 'admin', 0),
  ('55555555-5555-5555-5555-555555555555'::uuid, 'c06target@test.local', 'user', 100);

-- Switch to authenticated, set JWT claim to the admin's UUID.
SET LOCAL ROLE authenticated;
SET LOCAL request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';

-- Call admin_set_credits.
SELECT public.admin_set_credits(
  '55555555-5555-5555-5555-555555555555'::uuid,
  250,
  false
);

-- Check audit_log (must be readable as service_role; switch back).
RESET ROLE;
RESET request.jwt.claim.sub;

DO $$
DECLARE
  n int;
BEGIN
  SELECT count(*) INTO n
  FROM public.audit_log
  WHERE action = 'admin.set_credits'
    AND target_id = '55555555-5555-5555-5555-555555555555'
    AND actor_id = '44444444-4444-4444-4444-444444444444';
  IF n < 1 THEN
    RAISE EXCEPTION 'C06 FAIL: admin_set_credits did not write audit_log (n=%)', n;
  END IF;
END $$;

ROLLBACK;

SELECT 'C06 PASS: admin_set_credits writes audit_log' AS result;
