-- Revert 006 — restore the original (pre-B-04) refund_credits RPC.
-- WARNING: the original RPC incorrectly *added* credits instead of debiting.
-- Only apply this revert if 006 is being rolled back deliberately.

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
  v_bucket_id     uuid;
  v_existing_tx   uuid;
  v_balance_after integer;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'refund_credits: p_credits must be > 0';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  SELECT id INTO v_existing_tx
    FROM public.credit_transactions
   WHERE type = 'refund' AND ref_type = p_ref_type AND ref_id = p_ref_id
   LIMIT 1;
  IF v_existing_tx IS NOT NULL THEN
    RETURN jsonb_build_object('status','duplicate','transaction_id', v_existing_tx);
  END IF;

  INSERT INTO public.credit_buckets
    (user_id, source, original_credits, remaining_credits, ref_type, ref_id)
  VALUES
    (p_user_id, 'refund', p_credits, p_credits, p_ref_type, p_ref_id)
  RETURNING id INTO v_bucket_id;

  SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
    FROM public.credit_buckets WHERE user_id = p_user_id;

  INSERT INTO public.credit_transactions
    (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
  VALUES
    (p_user_id, 'refund', p_credits, v_bucket_id, p_ref_type, p_ref_id, v_balance_after,
     jsonb_build_object('source','refund'));

  RETURN jsonb_build_object('status','refunded','bucket_id', v_bucket_id, 'balance_after', v_balance_after);
END;
$$;

REVOKE ALL ON FUNCTION public.refund_credits(uuid, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text) TO service_role;
