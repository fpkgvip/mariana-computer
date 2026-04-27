-- Migration 018: I-01 add_credits advisory lock
-- I-01: add_credits lacked pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))
-- used by every sibling ledger function (refund_credits, grant_credits, spend_credits).
-- Without the lock, add_credits could read v_open_total from a stale snapshot before a
-- concurrent refund_credits commits its credit_clawbacks row, resulting in double-credit.
-- Fix: acquire the per-user advisory lock immediately after input validation and before
-- any reads, matching the pattern in 009_f03_refund_debt.sql lines 101 and 231.

CREATE OR REPLACE FUNCTION public.add_credits(p_user_id uuid, p_credits integer)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
DECLARE
  v_open_total   integer;
  v_net_addition integer;
  v_clawback     record;
  v_satisfy      integer;
  v_to_net       integer;
BEGIN
  IF p_credits < 0 THEN
    RAISE EXCEPTION 'Credits amount must be non-negative, got %', p_credits;
  END IF;

  -- I-01 fix: per-user serialization, matching refund_credits / grant_credits.
  -- Must be acquired BEFORE reading v_open_total so that any concurrent
  -- refund_credits that inserted a credit_clawbacks row has either committed
  -- (visible here) or is blocked waiting for this lock to release.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Calculate total open clawback amount for this user.
  SELECT COALESCE(SUM(amount), 0) INTO v_open_total
    FROM public.credit_clawbacks
   WHERE user_id = p_user_id
     AND satisfied_at IS NULL;

  -- Net addition: how many credits actually land in the user's balance.
  v_net_addition := GREATEST(0, p_credits - v_open_total);
  v_to_net       := p_credits;  -- tokens to consume from clawbacks

  -- Satisfy clawbacks oldest-first from the incoming tokens.
  FOR v_clawback IN
    SELECT id, amount
      FROM public.credit_clawbacks
     WHERE user_id = p_user_id
       AND satisfied_at IS NULL
     ORDER BY created_at ASC
     FOR UPDATE
  LOOP
    EXIT WHEN v_to_net <= 0;

    v_satisfy  := LEAST(v_to_net, v_clawback.amount);
    v_to_net   := v_to_net - v_satisfy;

    IF v_satisfy >= v_clawback.amount THEN
      UPDATE public.credit_clawbacks
         SET satisfied_at = now()
       WHERE id = v_clawback.id;
    ELSE
      UPDATE public.credit_clawbacks
         SET amount = amount - v_satisfy
       WHERE id = v_clawback.id;
    END IF;
  END LOOP;

  -- Add only the net (non-clawback-consumed) amount to profiles.tokens.
  UPDATE public.profiles
     SET tokens = tokens + v_net_addition,
         updated_at = now()
   WHERE id = p_user_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'User not found: %', p_user_id;
  END IF;
END;
$$;
