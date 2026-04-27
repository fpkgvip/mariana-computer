-- ============================================================
-- Migration 021 — K-02: atomic per-charge reversal RPC
-- ============================================================
-- Bug K-02 (Phase E re-audit #6):
-- _reverse_credits_for_charge in api.py performed:
--   1. SELECT existing dedup row (by reversal_key)
--   2. SELECT SUM(credits) for this charge_id
--   3. compute incremental = target - already_reversed
--   4. POST refund_credits RPC
--   5. POST INSERT dedup row
-- Two concurrent webhooks for the same charge with distinct event_ids both
-- pass step 1 (different reversal_keys), both observe already_reversed=N at
-- step 2 (neither has inserted its dedup row yet), both compute non-overlapping
-- incremental debits at step 3, and the RPC at step 4 does not collapse them
-- because ref_id differs. Net cumulative debit > true cumulative refund.
--
-- Fix: a single SECURITY DEFINER PL/pgSQL function that takes a per-charge
-- pg_advisory_xact_lock at entry, performs dedup + sum + refund_credits +
-- INSERT atomically inside one transaction, and releases the lock at commit.
-- The two-step "check then act" race is gone because the second concurrent
-- caller blocks on the lock until the first commits, then re-reads the sum
-- (now including the first's dedup row) and computes the correct delta.
--
-- Lock keys (verified non-conflicting):
--   per-charge: hashtextextended('charge:' || p_charge_id, 0)
--   per-user:   hashtextextended(p_user_id::text, 0)  -- existing in refund_credits
-- The string 'charge:' || charge_id never collides with a UUID text because
-- UUIDs do not start with 'charge:'.
--
-- Lock acquisition order: per-charge first, then per-user (via refund_credits).
-- Nothing else in the codebase acquires per-user before per-charge, so no
-- deadlock cycle exists.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION public.process_charge_reversal(
  p_user_id            uuid,
  p_charge_id          text,
  p_dispute_id         text,
  p_payment_intent_id  text,
  p_reversal_key       text,
  p_target_credits     integer,
  p_first_event_id     text,
  p_first_event_type   text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_already_reversed integer;
  v_incremental      integer;
  v_inserted_count   integer := 0;
  v_refund_result    jsonb;
BEGIN
  -- Validate inputs.
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'process_charge_reversal: p_user_id required';
  END IF;
  IF p_charge_id IS NULL OR length(p_charge_id) = 0 THEN
    RAISE EXCEPTION 'process_charge_reversal: p_charge_id required';
  END IF;
  IF p_reversal_key IS NULL OR length(p_reversal_key) = 0 THEN
    RAISE EXCEPTION 'process_charge_reversal: p_reversal_key required';
  END IF;
  IF p_target_credits IS NULL OR p_target_credits < 0 THEN
    RAISE EXCEPTION 'process_charge_reversal: p_target_credits must be >= 0';
  END IF;

  -- K-02 fix: per-charge serialization. Held until transaction commit.
  -- Distinct from the per-user lock taken inside refund_credits (different
  -- key shape, no collision). Two concurrent calls on the same charge
  -- serialize here; the second waits until the first commits its dedup
  -- INSERT, then computes the correct incremental delta.
  PERFORM pg_advisory_xact_lock(hashtextextended('charge:' || p_charge_id, 0));

  -- Dedup: if a row already exists for this exact reversal_key (e.g., a
  -- replay of the same webhook event_id), short-circuit.
  IF EXISTS (
    SELECT 1 FROM public.stripe_dispute_reversals
     WHERE reversal_key = p_reversal_key
  ) THEN
    RETURN jsonb_build_object('status', 'duplicate', 'credits', 0);
  END IF;

  -- Sum credits already reversed for this charge across all reversal_keys
  -- (distinct refund events, prior dispute events, etc.). Now safe because
  -- any concurrent process_charge_reversal call for the same charge is
  -- waiting on the per-charge lock above.
  SELECT COALESCE(SUM(credits), 0) INTO v_already_reversed
    FROM public.stripe_dispute_reversals
   WHERE charge_id = p_charge_id;

  v_incremental := GREATEST(0, p_target_credits - v_already_reversed);

  -- Always insert the dedup row first so that any future call for the same
  -- reversal_key (whether incremental was 0 or positive) short-circuits.
  -- The unique constraint on reversal_key collapses concurrent retries on
  -- the same key — but the per-charge advisory lock above already prevents
  -- that interleave.
  INSERT INTO public.stripe_dispute_reversals
    (reversal_key, user_id, charge_id, dispute_id, payment_intent_id,
     credits, first_event_id, first_event_type)
  VALUES
    (p_reversal_key, p_user_id, p_charge_id, p_dispute_id, p_payment_intent_id,
     v_incremental, p_first_event_id, p_first_event_type)
  ON CONFLICT (reversal_key) DO NOTHING;
  GET DIAGNOSTICS v_inserted_count = ROW_COUNT;

  IF v_inserted_count = 0 THEN
    -- Another caller raced past the EXISTS check (only possible if the
    -- per-charge lock did not actually serialize them — should never
    -- happen, but treat as duplicate defensively).
    RETURN jsonb_build_object('status', 'duplicate', 'credits', 0);
  END IF;

  IF v_incremental <= 0 THEN
    RETURN jsonb_build_object(
      'status', 'already_satisfied',
      'credits', 0,
      'already_reversed', v_already_reversed
    );
  END IF;

  -- Call refund_credits in the SAME transaction. refund_credits acquires
  -- pg_advisory_xact_lock on the user — that lock is acquired AFTER our
  -- per-charge lock, so the lock order is: charge → user. No other code
  -- path takes user before charge, so no deadlock cycle.
  -- refund_credits also dedups on (type='refund', ref_type, ref_id) — we
  -- pass ref_id = p_reversal_key for that purpose.
  v_refund_result := public.refund_credits(
    p_user_id   := p_user_id,
    p_credits   := v_incremental,
    p_ref_type  := 'stripe_event',
    p_ref_id    := p_reversal_key
  );

  RETURN jsonb_build_object(
    'status',           'reversed',
    'credits',          v_incremental,
    'already_reversed', v_already_reversed,
    'refund_result',    v_refund_result
  );
END;
$$;

-- Privilege hardening: only service_role may call. The webhook handler in
-- api.py runs with the service-role key.
REVOKE ALL ON FUNCTION
  public.process_charge_reversal(uuid, text, text, text, text, integer, text, text)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION
  public.process_charge_reversal(uuid, text, text, text, text, integer, text, text)
  FROM anon;
REVOKE ALL ON FUNCTION
  public.process_charge_reversal(uuid, text, text, text, text, integer, text, text)
  FROM authenticated;
GRANT EXECUTE ON FUNCTION
  public.process_charge_reversal(uuid, text, text, text, text, integer, text, text)
  TO service_role;

-- Post-apply invariants.
DO $post$
DECLARE
  bad text := '';
  hostile_roles text[] := ARRAY['public','anon','authenticated'];
  rl text;
BEGIN
  FOREACH rl IN ARRAY hostile_roles LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.routine_privileges
      WHERE specific_schema = 'public'
        AND routine_name = 'process_charge_reversal'
        AND grantee = CASE WHEN rl = 'public' THEN 'PUBLIC' ELSE rl END
        AND privilege_type = 'EXECUTE'
    ) THEN
      bad := bad || format('  process_charge_reversal: role %s still has EXECUTE%s',
                           rl, E'\n');
    END IF;
  END LOOP;

  IF length(bad) > 0 THEN
    RAISE EXCEPTION '021 FAIL: hostile EXECUTE grants remain:%s%s', E'\n', bad;
  END IF;

  RAISE NOTICE '021 invariants: process_charge_reversal grants OK';
END
$post$;

COMMIT;
