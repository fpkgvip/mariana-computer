-- ============================================================
-- Migration 024 — BB-01: refund_credits aggregate ledger row
-- ============================================================
-- Phase E re-audit #31 (A36) found that the FIFO bucket-debit loop in
-- refund_credits (defined in 009_f03_refund_debt.sql:155-160) inserts
-- one credit_transactions row PER bucket touched, all sharing
-- (p_ref_type, p_ref_id, type='refund').  The unique index
-- uq_credit_tx_idem from 004b_credit_tx_idem_concurrent.sql covers
-- (ref_type, ref_id, type) WHERE type IN ('grant','refund','expiry')
-- — so the second loop iteration violates the constraint and the
-- function aborts with UniqueViolation.
--
-- The 004b migration's own comment explicitly excludes type='spend'
-- because spend writes per-bucket; the same exclusion is required for
-- 'refund' but was overlooked.  Affected paths: Stripe refund webhook
-- (B-04), K-02 dispute reversal, U-01 OOO reversal handler, and the
-- AA-01 orphan-overrun path — all break on multi-bucket users.
--
-- Fix: collapse the per-bucket INSERT into a single aggregate ledger
-- row per refund_credits call.  Per-bucket movement is still recorded
-- in credit_buckets.remaining_credits; the credit_transactions row is
-- now an aggregate that matches the dedup contract in
-- uq_credit_tx_idem.  This mirrors grant_credits which has always
-- written one ledger row per call.
--
-- Idempotency contract preserved:
--   * Existing-tx and existing-clawback short-circuits at the top
--     return ('duplicate', transaction_id) / ('duplicate', clawback_id)
--     unchanged.
--   * Per-user advisory lock acquired BEFORE any read.
--   * Aggregate INSERT happens after the loop; the unique index
--     enforces at-most-one row per (ref_type, ref_id, 'refund').
--   * Deficit handling, profiles.tokens sync, and return shape all
--     unchanged.
--
-- Pre-flight: zero pre-existing rows where multiple credit_transactions
-- rows share (ref_type, ref_id, type='refund') — confirmed by the
-- existence of uq_credit_tx_idem (any prior duplicates would have been
-- impossible to insert).  No data backfill needed.

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
  v_total_balance  integer;
  v_to_debit_now   integer;
  v_deficit        integer;
  v_remaining      integer;
  v_bucket         record;
  v_take           integer;
  v_balance_after  integer;
  v_existing_tx    uuid;
  v_existing_cb    uuid;
  v_first_bucket   uuid;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'refund_credits: p_credits must be > 0';
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'refund_credits: p_user_id required';
  END IF;

  -- Per-user serialization (same lock key as grant/spend).
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Idempotency: check credit_transactions and credit_clawbacks.
  SELECT id INTO v_existing_tx
    FROM public.credit_transactions
   WHERE type = 'refund' AND ref_type = p_ref_type AND ref_id = p_ref_id
   LIMIT 1;
  IF v_existing_tx IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'duplicate', 'transaction_id', v_existing_tx);
  END IF;

  SELECT id INTO v_existing_cb
    FROM public.credit_clawbacks
   WHERE ref_type = p_ref_type AND ref_id = p_ref_id
   LIMIT 1;
  IF v_existing_cb IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'duplicate', 'clawback_id', v_existing_cb);
  END IF;

  -- Current balance (non-expired buckets only).
  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_total_balance
    FROM public.credit_buckets
   WHERE user_id = p_user_id
     AND remaining_credits > 0
     AND (expires_at IS NULL OR expires_at > clock_timestamp());

  -- How much we can debit immediately vs. how much becomes a deficit.
  v_to_debit_now := LEAST(v_total_balance, p_credits);
  v_deficit      := p_credits - v_to_debit_now;
  v_remaining    := v_to_debit_now;

  -- BB-01 fix: FIFO debit loop NO LONGER inserts per-bucket
  -- credit_transactions rows.  The per-bucket movement is recorded in
  -- credit_buckets.remaining_credits; the aggregate ledger row is
  -- inserted ONCE after the loop so it matches the uq_credit_tx_idem
  -- dedup contract.
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

    IF v_first_bucket IS NULL THEN
      v_first_bucket := v_bucket.id;
    END IF;

    v_remaining := v_remaining - v_take;
  END LOOP;

  -- Compute final balance after all bucket debits.
  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_balance_after
    FROM public.credit_buckets
   WHERE user_id = p_user_id;

  IF v_balance_after IS NULL THEN
    v_balance_after := 0;
  END IF;

  -- BB-01: aggregate ledger row.  Inserted only when actual credits
  -- were debited; deficit-only refunds (balance was 0) skip this and
  -- record a clawback row below — same as the prior contract.
  -- The credits column has CHECK (credits > 0), so we only INSERT when
  -- v_to_debit_now > 0.
  IF v_to_debit_now > 0 THEN
    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'refund', v_to_debit_now, v_first_bucket, p_ref_type, p_ref_id,
       v_balance_after,
       jsonb_build_object(
         'source', 'stripe_refund',
         'deficit', v_deficit,
         'aggregate', true
       ));
  END IF;

  -- Record any unsatisfied portion as a clawback row.
  -- This is the F-03 contract preserved: we do not silently forgive
  -- the deficit.
  IF v_deficit > 0 THEN
    INSERT INTO public.credit_clawbacks
      (user_id, amount, ref_type, ref_id)
    VALUES
      (p_user_id, v_deficit, p_ref_type, p_ref_id);
  END IF;

  -- B-05 sync: mirror the actual debit on profiles.tokens.
  UPDATE public.profiles
     SET tokens = GREATEST(0, tokens - v_to_debit_now),
         updated_at = now()
   WHERE id = p_user_id;

  RETURN jsonb_build_object(
    'status',           CASE WHEN v_deficit > 0 THEN 'deficit_recorded' ELSE 'reversed' END,
    'debited_now',      v_to_debit_now,
    'deficit_recorded', v_deficit,
    'balance_after',    v_balance_after
  );
END;
$$;

-- Re-apply the same grants so they survive the CREATE OR REPLACE.
REVOKE ALL ON FUNCTION public.refund_credits(uuid, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text) TO service_role;
