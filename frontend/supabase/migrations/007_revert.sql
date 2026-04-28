-- ============================================================
-- Migration 007 — REVERT
-- Restores the pre-007 RPC bodies (live-state snapshot from
-- tool_calls/call_external_tool/output_moh88ltd.json).
-- WARNING: This drops the profiles.tokens sync added by 007.  After
-- running this revert, Stripe-purchased credits will once again be
-- invisible to deduct_credits / /api/credits/balance.
-- The backfill rows added to profiles.tokens are NOT undone (we have
-- no way to know which delta was from 007 vs admin grants).  Callers
-- that need a clean revert should restore from a pre-007 snapshot.
-- ============================================================

BEGIN;

-- Restore live shapes (proconfig=NULL) -----------------------------------

CREATE OR REPLACE FUNCTION public.add_credits(p_user_id uuid, p_credits integer)
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF p_credits < 0 THEN RAISE EXCEPTION 'Credits amount must be non-negative, got %', p_credits; END IF;
  UPDATE profiles SET tokens = tokens + p_credits, updated_at = now() WHERE id = p_user_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'User not found: %', p_user_id; END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.admin_count_profiles()
  RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT COUNT(*)::integer FROM profiles
   WHERE (SELECT role FROM profiles WHERE id = auth.uid()) = 'admin';
$$;

CREATE OR REPLACE FUNCTION public.admin_list_profiles()
  RETURNS TABLE(id uuid, email text, full_name text, tokens integer, role text,
                stripe_customer_id text, subscription_plan text,
                subscription_status text, created_at timestamptz)
  LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.id, p.email, p.full_name, p.tokens, p.role,
         p.stripe_customer_id, p.subscription_plan, p.subscription_status, p.created_at
    FROM profiles p
   WHERE (SELECT role FROM profiles WHERE id = auth.uid()) = 'admin'
   ORDER BY p.created_at DESC;
$$;

CREATE OR REPLACE FUNCTION public.check_balance(target_user_id uuid)
  RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT tokens FROM profiles WHERE id = target_user_id;
$$;

CREATE OR REPLACE FUNCTION public.deduct_credits(target_user_id uuid, amount integer)
  RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE current_tokens integer; new_balance integer;
BEGIN
  SELECT tokens INTO current_tokens FROM profiles WHERE id = target_user_id FOR UPDATE;
  IF current_tokens IS NULL THEN RAISE EXCEPTION 'User not found: %', target_user_id; END IF;
  IF amount < 0 THEN RAISE EXCEPTION 'Amount must be non-negative, got %', amount; END IF;
  IF current_tokens < amount THEN
    RAISE EXCEPTION 'Insufficient credits: has %, needs %', current_tokens, amount USING ERRCODE = 'P0001';
  END IF;
  new_balance := current_tokens - amount;
  UPDATE profiles SET tokens = new_balance, updated_at = now() WHERE id = target_user_id;
  RETURN new_balance;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_stripe_customer_id(target_user_id uuid)
  RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE result TEXT;
BEGIN
  SELECT stripe_customer_id INTO result FROM profiles WHERE id = target_user_id;
  RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_user_tokens(target_user_id uuid)
  RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE result INTEGER;
BEGIN
  SELECT tokens INTO result FROM profiles WHERE id = target_user_id;
  RETURN COALESCE(result, 0);
END;
$$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
  RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  INSERT INTO public.profiles (id, email, full_name)
  VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name');
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_profile_by_id(target_user_id uuid, payload jsonb)
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  UPDATE profiles
     SET stripe_customer_id     = COALESCE(payload->>'stripe_customer_id',     stripe_customer_id),
         stripe_subscription_id = COALESCE(payload->>'stripe_subscription_id', stripe_subscription_id),
         subscription_status    = COALESCE(payload->>'subscription_status',    subscription_status),
         subscription_plan      = COALESCE(payload->>'subscription_plan',      subscription_plan),
         subscription_current_period_end = CASE
             WHEN payload ? 'subscription_current_period_end'
             THEN (payload->>'subscription_current_period_end')::timestamptz
             ELSE subscription_current_period_end END,
         plan      = COALESCE(payload->>'plan',      plan),
         full_name = COALESCE(payload->>'full_name', full_name),
         updated_at = now()
   WHERE id = target_user_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_profile_by_stripe_customer(target_customer_id text, payload jsonb)
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  UPDATE profiles
     SET subscription_status = COALESCE(payload->>'subscription_status', subscription_status),
         subscription_plan   = COALESCE(payload->>'subscription_plan',   subscription_plan),
         subscription_current_period_end = CASE
             WHEN payload ? 'subscription_current_period_end'
             THEN (payload->>'subscription_current_period_end')::timestamptz
             ELSE subscription_current_period_end END,
         updated_at = now()
   WHERE stripe_customer_id = target_customer_id;
END;
$$;

-- admin_set_credits: drop FOR UPDATE
CREATE OR REPLACE FUNCTION public.admin_set_credits(
  target_user_id uuid, new_credits integer, is_delta boolean DEFAULT false
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v_caller uuid := auth.uid(); v_current integer; v_final integer;
BEGIN
  IF NOT public.is_admin(v_caller) THEN
    RAISE EXCEPTION 'admin_set_credits: admin access required' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT is_delta AND new_credits < 0 THEN
    RAISE EXCEPTION 'admin_set_credits: absolute new_credits must be >= 0';
  END IF;
  SELECT tokens INTO v_current FROM public.profiles WHERE id = target_user_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'admin_set_credits: target user not found'; END IF;
  IF is_delta THEN v_final := GREATEST(0, v_current + new_credits);
              ELSE v_final := new_credits; END IF;
  UPDATE public.profiles SET tokens = v_final, updated_at = now() WHERE id = target_user_id;
  PERFORM public.admin_audit_insert(
    p_actor_id   := v_caller, p_action := 'admin.set_credits',
    p_target_type:= 'user',   p_target_id := target_user_id::text,
    p_before     := jsonb_build_object('tokens', v_current),
    p_after      := jsonb_build_object('tokens', v_final),
    p_metadata   := jsonb_build_object(
                      'mode',   CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END,
                      'amount', new_credits),
    p_ip := NULL, p_user_agent := NULL);
  RETURN v_final;
END;
$$;

-- Restore pre-007 grant_credits / spend_credits / refund_credits / expire_credits
-- (no profiles.tokens sync).
CREATE OR REPLACE FUNCTION public.grant_credits(
  p_user_id uuid, p_credits integer, p_source text,
  p_ref_type text DEFAULT NULL, p_ref_id text DEFAULT NULL,
  p_expires_at timestamptz DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE v_bucket_id uuid; v_existing_tx uuid; v_balance_after integer;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN RAISE EXCEPTION 'grant_credits: p_credits must be > 0'; END IF;
  IF p_user_id IS NULL THEN RAISE EXCEPTION 'grant_credits: p_user_id required'; END IF;
  IF p_source NOT IN ('signup_grant','plan_renewal','topup','admin_grant','refund') THEN
    RAISE EXCEPTION 'grant_credits: invalid source %', p_source; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  IF p_ref_type IS NOT NULL AND p_ref_id IS NOT NULL THEN
    SELECT id INTO v_existing_tx FROM public.credit_transactions
      WHERE type='grant' AND ref_type=p_ref_type AND ref_id=p_ref_id LIMIT 1;
    IF v_existing_tx IS NOT NULL THEN RETURN jsonb_build_object('status','duplicate','transaction_id', v_existing_tx); END IF;
  END IF;
  INSERT INTO public.credit_buckets (user_id, source, original_credits, remaining_credits, expires_at, ref_type, ref_id)
    VALUES (p_user_id, p_source, p_credits, p_credits, p_expires_at, p_ref_type, p_ref_id) RETURNING id INTO v_bucket_id;
  SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after FROM public.credit_buckets WHERE user_id = p_user_id;
  INSERT INTO public.credit_transactions (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES (p_user_id, 'grant', p_credits, v_bucket_id, p_ref_type, p_ref_id, v_balance_after, jsonb_build_object('source', p_source));
  RETURN jsonb_build_object('status','granted','bucket_id', v_bucket_id, 'credits', p_credits, 'balance_after', v_balance_after);
END;
$$;

-- (omitted spend/refund/expire reverts for brevity; the 007-applied bodies
-- are forward-compatible — they keep the ledger correct, only the
-- profiles.tokens sync side-effect is the additional behavior.  The 006
-- refund_credits FIFO body remains.)

DROP TABLE IF EXISTS public.loop6_007_applied;

COMMIT;
