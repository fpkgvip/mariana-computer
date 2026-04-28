-- Revert migration 024 — restore the per-bucket refund_credits from 009.
-- Re-applies the function definition from 009_f03_refund_debt.sql:72-192
-- verbatim.  Note that running this revert reintroduces BB-01 — the
-- multi-bucket UniqueViolation.  Use only if BB-01's aggregate-row
-- behaviour needs to be rolled back for triage.

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
    RETURN jsonb_build_object('status', 'duplicate', 'transaction_id', v_existing_tx);
  END IF;

  SELECT id INTO v_existing_cb
    FROM public.credit_clawbacks
   WHERE ref_type = p_ref_type AND ref_id = p_ref_id
   LIMIT 1;
  IF v_existing_cb IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'duplicate', 'clawback_id', v_existing_cb);
  END IF;

  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_total_balance
    FROM public.credit_buckets
   WHERE user_id = p_user_id
     AND remaining_credits > 0
     AND (expires_at IS NULL OR expires_at > clock_timestamp());

  v_to_debit_now := LEAST(v_total_balance, p_credits);
  v_deficit      := p_credits - v_to_debit_now;
  v_remaining    := v_to_debit_now;

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

    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'refund', v_take, v_bucket.id, p_ref_type, p_ref_id,
       v_balance_after,
       jsonb_build_object('source', 'stripe_refund', 'deficit', v_deficit));

    v_remaining := v_remaining - v_take;
  END LOOP;

  IF v_balance_after IS NULL THEN
    v_balance_after := 0;
  END IF;

  IF v_deficit > 0 THEN
    INSERT INTO public.credit_clawbacks
      (user_id, amount, ref_type, ref_id)
    VALUES
      (p_user_id, v_deficit, p_ref_type, p_ref_id);
  END IF;

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

REVOKE ALL ON FUNCTION public.refund_credits(uuid, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text) TO service_role;
