-- =====================================================================
-- Deft v1.0 — Phase 3: Integer-only credit ledger with FIFO buckets
-- =====================================================================
-- Money invariant: 1 credit = $0.01. All amounts are non-negative integers.
-- Transactions are append-only (no UPDATE on transactions table).
-- Per-user serialization via pg_advisory_xact_lock(hashtext(user_id::text)).
-- Stripe webhook idempotency via stripe_events table.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. credit_buckets — FIFO buckets of credits (one per grant)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.credit_buckets (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  source              text        NOT NULL CHECK (source IN ('signup_grant','plan_renewal','topup','admin_grant','refund')),
  original_credits    integer     NOT NULL CHECK (original_credits >= 0),
  remaining_credits   integer     NOT NULL CHECK (remaining_credits >= 0),
  granted_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at          timestamptz,
  ref_type            text,
  ref_id              text,
  created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (remaining_credits <= original_credits),
  CHECK (expires_at IS NULL OR expires_at > granted_at)
);

CREATE INDEX IF NOT EXISTS idx_credit_buckets_user_fifo
  ON public.credit_buckets (user_id, granted_at ASC)
  WHERE remaining_credits > 0;

CREATE INDEX IF NOT EXISTS idx_credit_buckets_expiry
  ON public.credit_buckets (expires_at)
  WHERE remaining_credits > 0 AND expires_at IS NOT NULL;

ALTER TABLE public.credit_buckets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "credit_buckets_owner_select" ON public.credit_buckets;
CREATE POLICY "credit_buckets_owner_select"
  ON public.credit_buckets FOR SELECT
  USING (auth.uid() = user_id);

-- No INSERT/UPDATE/DELETE policies — only service_role (RPC) modifies.

-- ---------------------------------------------------------------------
-- 2. credit_transactions — append-only ledger
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.credit_transactions (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  type          text        NOT NULL CHECK (type IN ('grant','spend','refund','expiry')),
  credits       integer     NOT NULL CHECK (credits > 0),  -- magnitude only; sign is in `type`
  bucket_id     uuid        REFERENCES public.credit_buckets(id),
  ref_type      text,                                        -- e.g. 'task','stripe_event','admin_grant'
  ref_id        text,                                        -- e.g. task_id, stripe event_id
  balance_after integer     NOT NULL CHECK (balance_after >= 0),
  metadata      jsonb       DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_credit_tx_user_time
  ON public.credit_transactions (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_credit_tx_ref
  ON public.credit_transactions (ref_type, ref_id);

-- Idempotency: one grant per (ref_type, ref_id) when both are non-null.
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_tx_grant_ref
  ON public.credit_transactions (ref_type, ref_id)
  WHERE type = 'grant' AND ref_type IS NOT NULL AND ref_id IS NOT NULL;

ALTER TABLE public.credit_transactions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "credit_tx_owner_select" ON public.credit_transactions;
CREATE POLICY "credit_tx_owner_select"
  ON public.credit_transactions FOR SELECT
  USING (auth.uid() = user_id);

-- Append-only: no UPDATE policy. No DELETE policy. Service role bypasses RLS.

-- ---------------------------------------------------------------------
-- 3. stripe_events — webhook idempotency (one row per Stripe event.id)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.stripe_events (
  event_id      text        PRIMARY KEY,
  event_type    text        NOT NULL,
  user_id       uuid        REFERENCES auth.users(id) ON DELETE SET NULL,
  payload       jsonb       NOT NULL,
  processed_at  timestamptz NOT NULL DEFAULT now(),
  result        text        NOT NULL CHECK (result IN ('processed','skipped','error')),
  error         text
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_user
  ON public.stripe_events (user_id, processed_at DESC);

ALTER TABLE public.stripe_events ENABLE ROW LEVEL SECURITY;
-- Explicit deny for non-service callers; service_role bypasses RLS.
DROP POLICY IF EXISTS "stripe_events_deny_all" ON public.stripe_events;
CREATE POLICY "stripe_events_deny_all" ON public.stripe_events FOR SELECT USING (false);

-- ---------------------------------------------------------------------
-- 4. credit_balances view — fast balance reads
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW public.credit_balances
WITH (security_invoker = true) AS
SELECT
  user_id,
  COALESCE(SUM(remaining_credits), 0)::integer AS balance,
  COUNT(*) FILTER (WHERE remaining_credits > 0) AS active_buckets,
  MIN(expires_at) FILTER (WHERE remaining_credits > 0 AND expires_at IS NOT NULL) AS next_expiry
FROM public.credit_buckets
GROUP BY user_id;

COMMENT ON VIEW public.credit_balances IS
  'Per-user credit balance computed from non-empty FIFO buckets. Read-only. security_invoker=true so RLS of credit_buckets applies.';
GRANT SELECT ON public.credit_balances TO authenticated;

-- ---------------------------------------------------------------------
-- 5. RPC: grant_credits(user_id, credits, source, ref_type, ref_id, expires_at)
--    Idempotent on (ref_type, ref_id). SECURITY DEFINER, owned by postgres.
-- ---------------------------------------------------------------------
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
  -- Validate
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'grant_credits: p_credits must be > 0, got %', p_credits;
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'grant_credits: p_user_id required';
  END IF;
  IF p_source NOT IN ('signup_grant','plan_renewal','topup','admin_grant','refund') THEN
    RAISE EXCEPTION 'grant_credits: invalid source %', p_source;
  END IF;

  -- Per-user serialization
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Idempotency check
  IF p_ref_type IS NOT NULL AND p_ref_id IS NOT NULL THEN
    SELECT id INTO v_existing_tx
      FROM public.credit_transactions
     WHERE type = 'grant' AND ref_type = p_ref_type AND ref_id = p_ref_id
     LIMIT 1;
    IF v_existing_tx IS NOT NULL THEN
      RETURN jsonb_build_object('status','duplicate','transaction_id', v_existing_tx);
    END IF;
  END IF;

  -- Insert bucket
  INSERT INTO public.credit_buckets
    (user_id, source, original_credits, remaining_credits, expires_at, ref_type, ref_id)
  VALUES
    (p_user_id, p_source, p_credits, p_credits, p_expires_at, p_ref_type, p_ref_id)
  RETURNING id INTO v_bucket_id;

  -- Compute new balance
  SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
    FROM public.credit_buckets
   WHERE user_id = p_user_id;

  -- Append transaction
  INSERT INTO public.credit_transactions
    (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after,
     metadata)
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

REVOKE ALL ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz) TO service_role;

-- ---------------------------------------------------------------------
-- 6. RPC: spend_credits(user_id, credits, ref_type, ref_id)
--    Iterates buckets FIFO; rejects with insufficient_balance if short.
-- ---------------------------------------------------------------------
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
    RAISE EXCEPTION 'spend_credits: p_credits must be > 0, got %', p_credits;
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'spend_credits: p_user_id required';
  END IF;

  -- Per-user serialization
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Pre-check sufficient balance (avoid partial debit on shortage)
  SELECT COALESCE(SUM(remaining_credits),0) INTO v_total_balance
    FROM public.credit_buckets
   WHERE user_id = p_user_id
     AND remaining_credits > 0
     AND (expires_at IS NULL OR expires_at > clock_timestamp());

  IF v_total_balance < p_credits THEN
    RETURN jsonb_build_object(
      'status','insufficient_balance',
      'balance', v_total_balance,
      'requested', p_credits
    );
  END IF;

  -- Iterate FIFO, oldest first; expired buckets remain spendable until expiry sweeper runs
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

    -- Per-bucket spend transaction
    SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
      FROM public.credit_buckets
     WHERE user_id = p_user_id;

    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'spend', v_take, v_bucket.id, p_ref_type, p_ref_id, v_balance_after, p_metadata);

    v_spend_rows := v_spend_rows || jsonb_build_object('bucket_id', v_bucket.id, 'credits', v_take);
    v_remaining := v_remaining - v_take;
  END LOOP;

  IF v_remaining > 0 THEN
    -- Should not happen due to pre-check, but guard against expired-bucket race
    RAISE EXCEPTION 'spend_credits: failed to debit full amount, % credits remaining', v_remaining;
  END IF;

  RETURN jsonb_build_object(
    'status','spent',
    'credits', p_credits,
    'balance_after', v_balance_after,
    'buckets', v_spend_rows
  );
END;
$$;

REVOKE ALL ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb) TO service_role;

-- ---------------------------------------------------------------------
-- 7. RPC: refund_credits(user_id, credits, ref_type, ref_id)
--    Creates a new bucket with `source='refund'` and tx of type='refund'.
-- ---------------------------------------------------------------------
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

  -- Idempotency on (ref_type, ref_id) for refunds — distinct from grants by tx type.
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

-- ---------------------------------------------------------------------
-- 8. RPC: expire_credits()
--    Sweeps expired buckets to zero; appends 'expiry' transactions.
--    Designed for pg_cron (out of scope of this migration).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.expire_credits() RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_count integer := 0;
  v_b record;
  v_balance_after integer;
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

    UPDATE public.credit_buckets SET remaining_credits = 0 WHERE id = v_b.id;

    SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
      FROM public.credit_buckets WHERE user_id = v_b.user_id;

    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (v_b.user_id, 'expiry', v_b.remaining_credits, v_b.id, 'bucket', v_b.id::text,
       v_balance_after, jsonb_build_object('source','expiry'));

    v_count := v_count + 1;
  END LOOP;

  RETURN v_count;
END;
$$;

REVOKE ALL ON FUNCTION public.expire_credits() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.expire_credits() TO service_role;

-- ---------------------------------------------------------------------
-- 9. RPC: get_my_balance() — authenticated read
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_my_balance() RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_user uuid := auth.uid();
  v_balance integer;
  v_next_expiry timestamptz;
BEGIN
  IF v_user IS NULL THEN
    RAISE EXCEPTION 'get_my_balance: not authenticated';
  END IF;

  SELECT
    COALESCE(SUM(remaining_credits),0)::integer,
    MIN(expires_at) FILTER (WHERE remaining_credits > 0 AND expires_at IS NOT NULL)
  INTO v_balance, v_next_expiry
  FROM public.credit_buckets
  WHERE user_id = v_user AND remaining_credits > 0;

  RETURN jsonb_build_object(
    'balance', v_balance,
    'next_expiry', v_next_expiry
  );
END;
$$;

REVOKE ALL ON FUNCTION public.get_my_balance() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_my_balance() TO authenticated;

-- ---------------------------------------------------------------------
-- 10. Auth hardening — RLS on profiles for UPDATE/DELETE (S-05)
-- ---------------------------------------------------------------------
DROP POLICY IF EXISTS "profiles_owner_update_safe" ON public.profiles;
CREATE POLICY "profiles_owner_update_safe"
  ON public.profiles FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    auth.uid() = id
    -- Lock down sensitive columns: users cannot self-modify role, plan, tokens, stripe_*, suspended_*
    -- by re-reading the row and asserting equality on protected columns.
    AND role           = (SELECT role           FROM public.profiles p WHERE p.id = auth.uid())
    AND plan           = (SELECT plan           FROM public.profiles p WHERE p.id = auth.uid())
    AND tokens         = (SELECT tokens         FROM public.profiles p WHERE p.id = auth.uid())
    AND COALESCE(stripe_customer_id,'')     = COALESCE((SELECT stripe_customer_id     FROM public.profiles p WHERE p.id = auth.uid()),'')
    AND COALESCE(stripe_subscription_id,'') = COALESCE((SELECT stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid()),'')
    AND COALESCE(suspended_at::text,'')     = COALESCE((SELECT suspended_at::text     FROM public.profiles p WHERE p.id = auth.uid()),'')
  );

-- No DELETE policy on profiles — only service role.

-- ---------------------------------------------------------------------
-- Done.
-- ---------------------------------------------------------------------
