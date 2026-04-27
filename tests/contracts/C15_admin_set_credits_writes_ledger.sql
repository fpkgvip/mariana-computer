-- @bug-id: B-16
-- @sev: P2
-- @phase: 0
-- @slice: contracts
-- @deterministic: must PASS post-012
--
-- B-16: admin_set_credits MUST write credit_transactions + credit_buckets rows
-- on each credit grant (positive delta or absolute increase).
-- Baseline pre-012: no ledger rows written. Post-012: grant transaction exists.

BEGIN;

-- Seed admin and target.
INSERT INTO public.profiles (id, email, role, tokens) VALUES
  ('66666666-6666-6666-6666-666666666661'::uuid, 'c15admin@test.local', 'admin', 0),
  ('77777777-7777-7777-7777-777777777772'::uuid, 'c15target@test.local', 'user', 0);

-- Switch to authenticated role with admin JWT claim.
SET LOCAL ROLE authenticated;
SET LOCAL request.jwt.claim.sub = '66666666-6666-6666-6666-666666666661';

-- Call admin_set_credits (absolute set to 1000).
SELECT public.admin_set_credits(
  '77777777-7777-7777-7777-777777777772'::uuid,
  1000,
  false
);

-- Reset role to postgres for verification.
RESET ROLE;
RESET request.jwt.claim.sub;

-- Check credit_transactions row exists.
DO $$
DECLARE
  tx_count int;
  bucket_count int;
BEGIN
  SELECT count(*) INTO tx_count
  FROM public.credit_transactions
  WHERE user_id = '77777777-7777-7777-7777-777777777772'::uuid
    AND type = 'grant'
    AND credits = 1000;

  SELECT count(*) INTO bucket_count
  FROM public.credit_buckets
  WHERE user_id = '77777777-7777-7777-7777-777777777772'::uuid
    AND source = 'admin_grant'
    AND original_credits = 1000;

  IF tx_count < 1 THEN
    RAISE EXCEPTION 'C15 FAIL: admin_set_credits did not write credit_transactions row (found=%)', tx_count;
  END IF;

  IF bucket_count < 1 THEN
    RAISE EXCEPTION 'C15 FAIL: admin_set_credits did not write credit_buckets row (found=%)', bucket_count;
  END IF;
END $$;

ROLLBACK;

SELECT 'C15 PASS: admin_set_credits writes credit_transactions + credit_buckets' AS result;
