-- @bug-id: R2
-- @sev: high
-- @phase: 0
-- @slice: contracts
-- @deterministic: must FAIL on baseline, PASS post-004b
--
-- R2 functional: inserting two refund transactions with the same
-- (ref_type, ref_id) must violate the unique index. On baseline the
-- index is grant-only so this insert succeeds (bug). Post-004b it
-- must fail with unique_violation.

BEGIN;

-- Seed
INSERT INTO public.profiles (id, email)
VALUES ('22222222-2222-2222-2222-222222222222'::uuid, 'c04@test.local')
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.credit_buckets (id, user_id, source, original_credits, remaining_credits, ref_type, ref_id)
VALUES (
  '33333333-3333-3333-3333-333333333333'::uuid,
  '22222222-2222-2222-2222-222222222222'::uuid,
  'refund', 100, 100, 'stripe_charge', 'ch_test_idem_001'
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.credit_transactions (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after)
VALUES (
  '22222222-2222-2222-2222-222222222222'::uuid,
  'refund', 100,
  '33333333-3333-3333-3333-333333333333'::uuid,
  'stripe_charge', 'ch_test_idem_001', 100
);

-- Try the replay. On baseline (uq_credit_tx_grant_ref only covers 'grant'),
-- this second insert succeeds. Post-004b (uq_credit_tx_idem covers refund),
-- it raises unique_violation.
DO $$
BEGIN
  INSERT INTO public.credit_transactions (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after)
  VALUES (
    '22222222-2222-2222-2222-222222222222'::uuid,
    'refund', 100,
    '33333333-3333-3333-3333-333333333333'::uuid,
    'stripe_charge', 'ch_test_idem_001', 200
  );
  -- If we got here, the replay succeeded — that's the bug.
  RAISE EXCEPTION 'C04 FAIL: refund replay was NOT blocked (idempotency gap present)';
EXCEPTION WHEN unique_violation THEN
  RAISE NOTICE 'C04 PASS: refund replay correctly blocked by unique index';
END $$;

ROLLBACK;

SELECT 'C04 PASS: refund replay blocked' AS result;
