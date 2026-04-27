-- ============================================================
-- Revert Migration 008 — F-03 refund clawback debt construct
-- ============================================================
-- Restores refund_credits / grant_credits / add_credits to the
-- 007 (loop6 B-05 ledger sync) versions, which clamp the debit
-- to available balance without recording a clawback row.
-- Also drops the credit_clawbacks table and index.
-- ============================================================

BEGIN;

-- Restore credit_transactions.type CHECK to original (drop clawback_satisfy).
ALTER TABLE public.credit_transactions
  DROP CONSTRAINT IF EXISTS credit_transactions_type_check;
ALTER TABLE public.credit_transactions
  ADD CONSTRAINT credit_transactions_type_check
  CHECK (type = ANY (ARRAY['grant','spend','refund','expiry']));

-- Restore refund_credits to the Migration-007 version (balance-clamp semantics).
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
  v_debited       integer;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'refund_credits: p_credits must be > 0';
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'refund_credits: p_user_id required';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  SELECT id INTO v_existing_tx
    FROM public.credit_transactions
   WHERE type = 'refund' AND ref_type = p_ref_type AND ref_id = p_ref_id
   LIMIT 1;
  IF v_existing_tx IS NOT NULL THEN
    RETURN jsonb_build_object('status','duplicate','transaction_id', v_existing_tx);
  END IF;

  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_total_balance
    FROM public.credit_buckets
   WHERE user_id = p_user_id
     AND remaining_credits > 0
     AND (expires_at IS NULL OR expires_at > clock_timestamp());

  IF v_total_balance < p_credits THEN
    v_remaining := v_total_balance;
  END IF;

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
      FROM public.credit_buckets WHERE user_id = p_user_id;

    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'refund', v_take, v_bucket.id, p_ref_type, p_ref_id, v_balance_after,
       jsonb_build_object('source', 'stripe_refund'));

    v_remaining := v_remaining - v_take;
  END LOOP;

  v_debited := COALESCE(LEAST(p_credits, v_total_balance), 0);

  IF v_balance_after IS NULL THEN
    UPDATE public.profiles
       SET tokens = GREATEST(0, tokens - p_credits),
           updated_at = now()
     WHERE id = p_user_id;

    RETURN jsonb_build_object(
      'status',          'no_credits',
      'credits_debited', 0,
      'balance_after',   0
    );
  END IF;

  UPDATE public.profiles
     SET tokens = GREATEST(0, tokens - p_credits),
         updated_at = now()
   WHERE id = p_user_id;

  RETURN jsonb_build_object(
    'status',          'reversed',
    'credits_debited', v_debited,
    'balance_after',   v_balance_after
  );
END;
$$;

-- Restore grant_credits to the Migration-007 version (no clawback satisfaction).
CREATE OR REPLACE FUNCTION public.grant_credits(
  p_user_id    uuid,
  p_credits    integer,
  p_source     text,
  p_ref_type   text DEFAULT NULL,
  p_ref_id     text DEFAULT NULL,
  p_expires_at timestamptz DEFAULT NULL
) RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
DECLARE
  v_bucket_id     uuid;
  v_existing_tx   uuid;
  v_balance_after integer;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'grant_credits: p_credits must be > 0';
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'grant_credits: p_user_id required';
  END IF;
  IF p_source NOT IN ('signup_grant','plan_renewal','topup','admin_grant','refund') THEN
    RAISE EXCEPTION 'grant_credits: invalid source %', p_source;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  IF p_ref_type IS NOT NULL AND p_ref_id IS NOT NULL THEN
    SELECT id INTO v_existing_tx
      FROM public.credit_transactions
     WHERE type='grant' AND ref_type=p_ref_type AND ref_id=p_ref_id
     LIMIT 1;
    IF v_existing_tx IS NOT NULL THEN
      RETURN jsonb_build_object('status','duplicate','transaction_id', v_existing_tx);
    END IF;
  END IF;

  INSERT INTO public.credit_buckets
    (user_id, source, original_credits, remaining_credits, expires_at, ref_type, ref_id)
  VALUES
    (p_user_id, p_source, p_credits, p_credits, p_expires_at, p_ref_type, p_ref_id)
  RETURNING id INTO v_bucket_id;

  UPDATE public.profiles
     SET tokens = tokens + p_credits,
         updated_at = now()
   WHERE id = p_user_id;

  SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
    FROM public.credit_buckets WHERE user_id = p_user_id;

  INSERT INTO public.credit_transactions
    (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
  VALUES
    (p_user_id, 'grant', p_credits, v_bucket_id, p_ref_type, p_ref_id, v_balance_after,
     jsonb_build_object('source', p_source));

  RETURN jsonb_build_object(
    'status','granted',
    'bucket_id', v_bucket_id,
    'credits', p_credits,
    'balance_after', v_balance_after
  );
END;
$$;

-- Restore add_credits to the Migration-007 version (no clawback logic).
CREATE OR REPLACE FUNCTION public.add_credits(p_user_id uuid, p_credits integer)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  IF p_credits < 0 THEN
    RAISE EXCEPTION 'Credits amount must be non-negative, got %', p_credits;
  END IF;

  UPDATE public.profiles
     SET tokens = tokens + p_credits,
         updated_at = now()
   WHERE id = p_user_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'User not found: %', p_user_id;
  END IF;
END;
$$;

-- Re-apply privilege posture.
REVOKE ALL ON FUNCTION public.refund_credits(uuid, integer, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.add_credits(uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text)                   TO service_role;
GRANT EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz)  TO service_role;
GRANT EXECUTE ON FUNCTION public.add_credits(uuid, integer)                                   TO service_role;

-- Drop clawback table (CASCADE drops its index and policies).
DROP TABLE IF EXISTS public.credit_clawbacks CASCADE;

COMMIT;
