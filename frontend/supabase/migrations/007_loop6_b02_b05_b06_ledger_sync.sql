-- ============================================================
-- Migration 007 — Loop 6 zero-bug convergence
--   B-02: SET search_path on 10 SECURITY DEFINER functions
--   B-05: grant_credits / spend_credits / refund_credits / expire_credits
--         now also keep profiles.tokens in sync so Stripe-purchased
--         credits are immediately spendable via deduct_credits and the
--         user-facing balance (/api/credits/balance) reflects them.
--   B-06: admin_set_credits SELECT now uses FOR UPDATE (race-safe).
--
-- Backfill step (DML) at the end credits any pre-existing
-- credit_buckets.remaining_credits onto profiles.tokens, since prior
-- versions never sync'd Stripe grants into the spendable balance.
-- The backfill is one-shot (idempotent only on first run); a guard
-- table public.loop6_007_applied prevents accidental re-application.
-- ============================================================

BEGIN;

-- -----------------------------------------------------------
-- B-02: pin search_path on every SECURITY DEFINER function that
-- previously had proconfig=NULL.  Using `public, pg_temp` matches
-- the existing posture on grant_credits / spend_credits / refund_credits.
-- -----------------------------------------------------------

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

CREATE OR REPLACE FUNCTION public.admin_count_profiles()
  RETURNS integer
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
  SELECT COUNT(*)::integer
    FROM public.profiles
   WHERE (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'admin';
$$;

CREATE OR REPLACE FUNCTION public.admin_list_profiles()
  RETURNS TABLE(id uuid, email text, full_name text, tokens integer, role text,
                stripe_customer_id text, subscription_plan text,
                subscription_status text, created_at timestamptz)
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
  SELECT p.id, p.email, p.full_name, p.tokens, p.role,
         p.stripe_customer_id, p.subscription_plan, p.subscription_status,
         p.created_at
    FROM public.profiles p
   WHERE (SELECT role FROM public.profiles WHERE id = auth.uid()) = 'admin'
   ORDER BY p.created_at DESC;
$$;

CREATE OR REPLACE FUNCTION public.check_balance(target_user_id uuid)
  RETURNS integer
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
  SELECT tokens FROM public.profiles WHERE id = target_user_id;
$$;

-- B-02 + race-safety preserved: deduct_credits already had FOR UPDATE; we
-- only add SET search_path.
CREATE OR REPLACE FUNCTION public.deduct_credits(target_user_id uuid, amount integer)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
DECLARE
  current_tokens integer;
  new_balance    integer;
BEGIN
  SELECT tokens INTO current_tokens
    FROM public.profiles
   WHERE id = target_user_id
     FOR UPDATE;

  IF current_tokens IS NULL THEN
    RAISE EXCEPTION 'User not found: %', target_user_id;
  END IF;

  IF amount < 0 THEN
    RAISE EXCEPTION 'Amount must be non-negative, got %', amount;
  END IF;

  IF current_tokens < amount THEN
    RAISE EXCEPTION 'Insufficient credits: has %, needs %', current_tokens, amount
      USING ERRCODE = 'P0001';
  END IF;

  new_balance := current_tokens - amount;

  UPDATE public.profiles
     SET tokens = new_balance,
         updated_at = now()
   WHERE id = target_user_id;

  RETURN new_balance;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_stripe_customer_id(target_user_id uuid)
  RETURNS text
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
DECLARE result text;
BEGIN
  SELECT stripe_customer_id INTO result FROM public.profiles WHERE id = target_user_id;
  RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.get_user_tokens(target_user_id uuid)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
DECLARE result integer;
BEGIN
  SELECT tokens INTO result FROM public.profiles WHERE id = target_user_id;
  RETURN COALESCE(result, 0);
END;
$$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
  RETURNS trigger
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  INSERT INTO public.profiles (id, email, full_name)
  VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name');
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_profile_by_id(target_user_id uuid, payload jsonb)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  UPDATE public.profiles
     SET stripe_customer_id     = COALESCE(payload->>'stripe_customer_id',     stripe_customer_id),
         stripe_subscription_id = COALESCE(payload->>'stripe_subscription_id', stripe_subscription_id),
         subscription_status    = COALESCE(payload->>'subscription_status',    subscription_status),
         subscription_plan      = COALESCE(payload->>'subscription_plan',      subscription_plan),
         subscription_current_period_end = CASE
             WHEN payload ? 'subscription_current_period_end'
             THEN (payload->>'subscription_current_period_end')::timestamptz
             ELSE subscription_current_period_end
         END,
         plan      = COALESCE(payload->>'plan',      plan),
         full_name = COALESCE(payload->>'full_name', full_name),
         updated_at = now()
   WHERE id = target_user_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_profile_by_stripe_customer(target_customer_id text, payload jsonb)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
BEGIN
  UPDATE public.profiles
     SET subscription_status = COALESCE(payload->>'subscription_status', subscription_status),
         subscription_plan   = COALESCE(payload->>'subscription_plan',   subscription_plan),
         subscription_current_period_end = CASE
             WHEN payload ? 'subscription_current_period_end'
             THEN (payload->>'subscription_current_period_end')::timestamptz
             ELSE subscription_current_period_end
         END,
         updated_at = now()
   WHERE stripe_customer_id = target_customer_id;
END;
$$;

-- -----------------------------------------------------------
-- B-06: admin_set_credits — add FOR UPDATE to the SELECT so that
-- concurrent deduct_credits / admin_set_credits invocations cannot
-- race on the same target row.  Function already had search_path=''.
-- Body is otherwise identical to the live version.
-- -----------------------------------------------------------

CREATE OR REPLACE FUNCTION public.admin_set_credits(
  target_user_id uuid,
  new_credits    integer,
  is_delta       boolean DEFAULT false
) RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = ''
AS $$
DECLARE
  v_caller  uuid := auth.uid();
  v_current integer;
  v_final   integer;
BEGIN
  IF NOT public.is_admin(v_caller) THEN
    RAISE EXCEPTION 'admin_set_credits: admin access required'
      USING ERRCODE = 'insufficient_privilege';
  END IF;

  IF NOT is_delta AND new_credits < 0 THEN
    RAISE EXCEPTION 'admin_set_credits: absolute new_credits must be >= 0';
  END IF;

  -- B-06 fix: lock the row before reading so concurrent updates serialize.
  SELECT tokens INTO v_current
    FROM public.profiles
   WHERE id = target_user_id
     FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'admin_set_credits: target user not found';
  END IF;

  IF is_delta THEN
    v_final := GREATEST(0, v_current + new_credits);
  ELSE
    v_final := new_credits;
  END IF;

  UPDATE public.profiles
     SET tokens = v_final,
         updated_at = now()
   WHERE id = target_user_id;

  PERFORM public.admin_audit_insert(
    p_actor_id   := v_caller,
    p_action     := 'admin.set_credits',
    p_target_type:= 'user',
    p_target_id  := target_user_id::text,
    p_before     := jsonb_build_object('tokens', v_current),
    p_after      := jsonb_build_object('tokens', v_final),
    p_metadata   := jsonb_build_object(
                      'mode',   CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END,
                      'amount', new_credits
                    ),
    p_ip         := NULL,
    p_user_agent := NULL
  );

  RETURN v_final;
END;
$$;

-- -----------------------------------------------------------
-- B-05: ledger functions now also sync profiles.tokens so the
-- spendable balance always reflects ledger activity.  This closes
-- the long-standing bifurcation where Stripe grants landed in
-- credit_buckets but were invisible to deduct_credits / balance.
--
-- Sync rules (additive, no semantics change):
--   grant_credits  → tokens += p_credits (after the duplicate guard)
--   spend_credits  → tokens -= p_credits (defensive; not currently called)
--   refund_credits → tokens -= credits actually debited
--   expire_credits → tokens -= each expired bucket's remaining_credits
--
-- The legacy deduct_credits / add_credits path (used for investigation
-- reservation/rollback) is unchanged.  After this migration both
-- balance views still use profiles.tokens as the source of truth.
-- -----------------------------------------------------------

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

  -- B-05 sync: keep profiles.tokens in lockstep with the ledger.  Best-effort:
  -- if the profile row is missing (orphan grant), the UPDATE is a no-op.
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

CREATE OR REPLACE FUNCTION public.spend_credits(
  p_user_id  uuid,
  p_credits  integer,
  p_ref_type text,
  p_ref_id   text,
  p_metadata jsonb DEFAULT '{}'::jsonb
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
  v_spend_rows    jsonb := '[]'::jsonb;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'spend_credits: p_credits must be > 0';
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'spend_credits: p_user_id required';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  SELECT COALESCE(SUM(remaining_credits),0) INTO v_total_balance
    FROM public.credit_buckets
   WHERE user_id=p_user_id
     AND remaining_credits>0
     AND (expires_at IS NULL OR expires_at > clock_timestamp());

  IF v_total_balance < p_credits THEN
    RETURN jsonb_build_object(
      'status','insufficient_balance',
      'balance', v_total_balance,
      'requested', p_credits
    );
  END IF;

  FOR v_bucket IN
    SELECT id, remaining_credits FROM public.credit_buckets
     WHERE user_id=p_user_id
       AND remaining_credits>0
       AND (expires_at IS NULL OR expires_at > clock_timestamp())
     ORDER BY granted_at ASC, id ASC
       FOR UPDATE
  LOOP
    EXIT WHEN v_remaining <= 0;
    v_take := LEAST(v_bucket.remaining_credits, v_remaining);

    UPDATE public.credit_buckets
       SET remaining_credits = remaining_credits - v_take
     WHERE id = v_bucket.id;

    SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
      FROM public.credit_buckets WHERE user_id = p_user_id;

    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'spend', v_take, v_bucket.id, p_ref_type, p_ref_id, v_balance_after, p_metadata);

    v_spend_rows := v_spend_rows || jsonb_build_object('bucket_id', v_bucket.id, 'credits', v_take);
    v_remaining  := v_remaining - v_take;
  END LOOP;

  IF v_remaining > 0 THEN
    RAISE EXCEPTION 'spend_credits: failed to debit full amount, % remaining', v_remaining;
  END IF;

  -- B-05 sync: mirror the spend on profiles.tokens.  Clamp at 0 to defend
  -- against any pre-existing drift from the legacy deduct_credits path.
  UPDATE public.profiles
     SET tokens = GREATEST(0, tokens - p_credits),
         updated_at = now()
   WHERE id = p_user_id;

  RETURN jsonb_build_object(
    'status','spent',
    'credits', p_credits,
    'balance_after', v_balance_after,
    'buckets', v_spend_rows
  );
END;
$$;

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

  -- credits actually debited from the ledger
  v_debited := COALESCE(LEAST(p_credits, v_total_balance), 0);

  IF v_balance_after IS NULL THEN
    -- No buckets existed; nothing was debited from the ledger.  We still
    -- decrement profiles.tokens by the full requested amount so the user
    -- cannot retain spendable credits for a charge that has been reversed.
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

  -- B-05 sync: mirror the ledger debit on profiles.tokens.  Even if
  -- v_total_balance < p_credits we still try to recover the full amount
  -- from profiles.tokens because admin grants may have inflated it.
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

CREATE OR REPLACE FUNCTION public.expire_credits()
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
AS $$
DECLARE
  v_count          integer := 0;
  v_b              record;
  v_balance_after  integer;
BEGIN
  FOR v_b IN
    SELECT id, user_id, remaining_credits
      FROM public.credit_buckets
     WHERE expires_at IS NOT NULL
       AND expires_at <= clock_timestamp()
       AND remaining_credits > 0
     ORDER BY user_id, granted_at
       FOR UPDATE
  LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended(v_b.user_id::text, 0));

    UPDATE public.credit_buckets
       SET remaining_credits = 0
     WHERE id = v_b.id;

    SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
      FROM public.credit_buckets WHERE user_id = v_b.user_id;

    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (v_b.user_id, 'expiry', v_b.remaining_credits, v_b.id, 'bucket', v_b.id::text,
       v_balance_after, jsonb_build_object('source','expiry'));

    -- B-05 sync: mirror the expiry on profiles.tokens.
    UPDATE public.profiles
       SET tokens = GREATEST(0, tokens - v_b.remaining_credits),
           updated_at = now()
     WHERE id = v_b.user_id;

    v_count := v_count + 1;
  END LOOP;
  RETURN v_count;
END;
$$;

-- Re-grant EXECUTE privileges so privilege posture matches 005 (split-revoke).
-- These RPCs were not anon-callable after 005, but CREATE OR REPLACE leaves
-- existing grants intact in Postgres.  We re-state the canonical set for
-- documentation and to keep the local baseline and live in lockstep.
REVOKE ALL ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz)  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb)              FROM PUBLIC;
REVOKE ALL ON FUNCTION public.refund_credits(uuid, integer, text, text)                    FROM PUBLIC;
REVOKE ALL ON FUNCTION public.expire_credits()                                             FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz)  TO service_role;
GRANT EXECUTE ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb)              TO service_role;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text)                    TO service_role;
GRANT EXECUTE ON FUNCTION public.expire_credits()                                             TO service_role;

-- -----------------------------------------------------------
-- One-shot backfill: reconcile profiles.tokens with credit_buckets.
--
-- Prior to 007, grant_credits did not update profiles.tokens.  Any user
-- who paid via Stripe therefore has bucket credits that are not in
-- their spendable balance.  We add them in here.  A guard table
-- (public.loop6_007_applied) prevents accidental re-application.
-- -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.loop6_007_applied (
  applied_at timestamptz NOT NULL DEFAULT now(),
  one_row    boolean PRIMARY KEY DEFAULT true,
  CHECK (one_row = true)
);

DO $$
DECLARE v_already boolean;
BEGIN
  SELECT EXISTS(SELECT 1 FROM public.loop6_007_applied) INTO v_already;
  IF NOT v_already THEN
    UPDATE public.profiles p
       SET tokens     = p.tokens + COALESCE(b.bucket_balance, 0),
           updated_at = now()
      FROM (
        SELECT user_id, SUM(remaining_credits)::integer AS bucket_balance
          FROM public.credit_buckets
         WHERE remaining_credits > 0
           AND (expires_at IS NULL OR expires_at > clock_timestamp())
         GROUP BY user_id
      ) b
     WHERE p.id = b.user_id
       AND COALESCE(b.bucket_balance, 0) > 0;

    INSERT INTO public.loop6_007_applied(one_row) VALUES (true);
  END IF;
END;
$$;

COMMIT;
