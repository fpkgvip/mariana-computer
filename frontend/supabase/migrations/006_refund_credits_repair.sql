-- ============================================================
-- Migration 006 — B-04: fix refund_credits RPC to DEBIT credits
-- ============================================================
-- The original refund_credits (from 002) added a new credit bucket
-- (source='refund') which *increased* a user's balance. That is wrong
-- for the Stripe refund/dispute use-case: when a payment is reversed,
-- the credits previously granted must be *removed* from the user's
-- balance. This migration replaces the RPC with a version that debits
-- the user's buckets FIFO (like spend_credits) but records a
-- type='refund' transaction row so the ledger audit trail is clear and
-- distinct from ordinary spend rows. Idempotency is preserved on
-- (ref_type, ref_id) for type='refund'.
--
-- NOTE: The old RPC accepted the same signature; we preserve it so
-- no Python call sites need to change.
-- ============================================================

CREATE OR REPLACE FUNCTION public.refund_credits(
  p_user_id  uuid,
  p_credits  integer,
  p_ref_type text,
  p_ref_id   text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_remaining     integer := p_credits;
  v_total_balance integer;
  v_bucket        record;
  v_take          integer;
  v_balance_after integer;
  v_existing_tx   uuid;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'refund_credits: p_credits must be > 0';
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'refund_credits: p_user_id required';
  END IF;

  -- Per-user serialization (same lock key as grant/spend).
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Idempotency: if a refund transaction with this (ref_type, ref_id) was
  -- already recorded, return immediately without double-debiting.
  SELECT id INTO v_existing_tx
    FROM public.credit_transactions
   WHERE type = 'refund' AND ref_type = p_ref_type AND ref_id = p_ref_id
   LIMIT 1;
  IF v_existing_tx IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'duplicate', 'transaction_id', v_existing_tx);
  END IF;

  -- Current balance (only non-expired buckets).
  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_total_balance
    FROM public.credit_buckets
   WHERE user_id = p_user_id
     AND remaining_credits > 0
     AND (expires_at IS NULL OR expires_at > clock_timestamp());

  -- If the user already has less than the requested debit, clamp to their
  -- actual balance so we don't error out — the credits were already spent.
  -- This is intentional: a refund should always succeed even if the user
  -- burned the credits before the chargeback arrived.
  IF v_total_balance < p_credits THEN
    v_remaining := v_total_balance;
  END IF;

  -- Debit FIFO buckets (oldest first), same ordering as spend_credits.
  FOR v_bucket IN
    SELECT id, remaining_credits
      FROM public.credit_buckets
     WHERE user_id = p_user_id
       AND remaining_credits > 0
       AND (expires_at IS NULL OR expires_at > clock_timestamp())
     ORDER BY granted_at ASC, id ASC
     FOR UPDATE
  LOOP
    EXIT WHEN v_remaining <= 0;

    v_take := LEAST(v_bucket.remaining_credits, v_remaining);

    UPDATE public.credit_buckets
       SET remaining_credits = remaining_credits - v_take
     WHERE id = v_bucket.id;

    SELECT COALESCE(SUM(remaining_credits), 0) INTO v_balance_after
      FROM public.credit_buckets
     WHERE user_id = p_user_id;

    -- Record a 'refund' debit transaction per bucket touched.
    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'refund', v_take, v_bucket.id, p_ref_type, p_ref_id, v_balance_after,
       jsonb_build_object('source', 'stripe_refund'));

    v_remaining := v_remaining - v_take;
  END LOOP;

  -- If there were no buckets at all (balance was already 0), record a
  -- zero-credit sentinel row so idempotency still functions correctly.
  -- We use credits=0... but the CHECK constraint requires credits > 0.
  -- Instead, if nothing was debited, just return 'no_credits' without
  -- inserting a row; the caller logs this as a warning.
  IF v_balance_after IS NULL THEN
    RETURN jsonb_build_object(
      'status',         'no_credits',
      'credits_debited', 0,
      'balance_after',   0
    );
  END IF;

  RETURN jsonb_build_object(
    'status',          'reversed',
    'credits_debited', p_credits - v_remaining,
    'balance_after',   v_balance_after
  );
END;
$$;

-- Re-apply the same grants so they survive the CREATE OR REPLACE.
REVOKE ALL ON FUNCTION public.refund_credits(uuid, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text) TO service_role;
