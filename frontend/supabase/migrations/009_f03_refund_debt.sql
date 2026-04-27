-- ============================================================
-- Migration 008 — F-03: Refund clawback debt construct
-- ============================================================
-- Finding F-03 (Phase E re-audit): when a user spends credits
-- before a refund/dispute clawback arrives, the current
-- refund_credits RPC clamps the debit to the available balance
-- and silently forgives the rest — an economic loss vector.
--
-- Fix: introduce credit_clawbacks table to record unsatisfied
-- refund deficits that persist and are charged against future
-- grants/top-ups before any new spend is allowed.
--
-- Key changes:
--   1. CREATE TABLE public.credit_clawbacks
--   2. REPLACE refund_credits: debit what's available, record
--      deficit as a clawback row (instead of clamping/forgiving)
--   3. REPLACE grant_credits: after bucket creation, satisfy
--      any open clawbacks FIFO before returning to caller
--   4. REPLACE add_credits (profiles.tokens only): net the
--      addition by any open clawback total
--   5. RLS + permission hardening throughout
-- ============================================================

BEGIN;

-- ============================================================
-- 0. Extend credit_transactions.type CHECK to allow 'clawback_satisfy'
--    (new audit type for when a grant satisfies a clawback deficit)
-- ============================================================

ALTER TABLE public.credit_transactions
  DROP CONSTRAINT IF EXISTS credit_transactions_type_check;
ALTER TABLE public.credit_transactions
  ADD CONSTRAINT credit_transactions_type_check
  CHECK (type = ANY (ARRAY['grant','spend','refund','expiry','clawback_satisfy']));

-- ============================================================
-- 1. credit_clawbacks table
-- ============================================================

CREATE TABLE IF NOT EXISTS public.credit_clawbacks (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  amount       integer     NOT NULL CHECK (amount > 0),
  ref_type     text        NOT NULL,
  ref_id       text        NOT NULL,
  satisfied_at timestamptz NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ref_type, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_credit_clawbacks_user_unsatisfied
  ON public.credit_clawbacks (user_id)
  WHERE satisfied_at IS NULL;

ALTER TABLE public.credit_clawbacks ENABLE ROW LEVEL SECURITY;

-- Users can SELECT their own clawback rows (for transparency).
DROP POLICY IF EXISTS "credit_clawbacks_owner_select" ON public.credit_clawbacks;
CREATE POLICY "credit_clawbacks_owner_select"
  ON public.credit_clawbacks FOR SELECT
  USING (auth.uid() = user_id);

-- Only service_role/postgres may INSERT/UPDATE/DELETE (via SECURITY DEFINER RPCs).
-- No INSERT/UPDATE/DELETE policies for authenticated/anon.

-- ============================================================
-- 2. Replace refund_credits — debit available balance AND
--    record any unsatisfied portion as a clawback row.
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

  -- Per-user serialization (same lock key as grant/spend).
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Idempotency: check credit_transactions (for partial refunds already started)
  -- AND credit_clawbacks (for deficit-only cases where no tx was written).
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

  -- FIFO debit loop for the immediately-available portion.
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

  -- If nothing was debited from buckets (balance was 0), balance_after stays NULL.
  IF v_balance_after IS NULL THEN
    v_balance_after := 0;
  END IF;

  -- Record any unsatisfied portion as a clawback row.
  -- This is the core F-03 fix: we no longer silently forgive the deficit.
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

-- ============================================================
-- 3. Replace grant_credits — after bucket creation, satisfy
--    any open clawbacks FIFO before returning to caller.
-- ============================================================

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
  v_bucket_id       uuid;
  v_existing_tx     uuid;
  v_balance_after   integer;
  v_clawback        record;
  v_available       integer;  -- remaining_credits still in the new bucket
  v_satisfy         integer;  -- amount to satisfy from this clawback row
  v_tokens_net      integer;  -- net tokens to add to profiles (after clawback drains)
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

  -- Per-user serialization.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Idempotency check.
  IF p_ref_type IS NOT NULL AND p_ref_id IS NOT NULL THEN
    SELECT id INTO v_existing_tx
      FROM public.credit_transactions
     WHERE type = 'grant' AND ref_type = p_ref_type AND ref_id = p_ref_id
     LIMIT 1;
    IF v_existing_tx IS NOT NULL THEN
      RETURN jsonb_build_object('status', 'duplicate', 'transaction_id', v_existing_tx);
    END IF;
  END IF;

  -- Insert the new credit bucket.
  INSERT INTO public.credit_buckets
    (user_id, source, original_credits, remaining_credits, expires_at, ref_type, ref_id)
  VALUES
    (p_user_id, p_source, p_credits, p_credits, p_expires_at, p_ref_type, p_ref_id)
  RETURNING id INTO v_bucket_id;

  -- Record the grant transaction.
  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_balance_after
    FROM public.credit_buckets WHERE user_id = p_user_id;

  INSERT INTO public.credit_transactions
    (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
  VALUES
    (p_user_id, 'grant', p_credits, v_bucket_id, p_ref_type, p_ref_id, v_balance_after,
     jsonb_build_object('source', p_source));

  -- ----------------------------------------------------------------
  -- Clawback satisfaction: drain any open deficits from this new
  -- bucket, oldest-first, before the caller sees the balance.
  -- ----------------------------------------------------------------
  v_available   := p_credits;  -- remaining credits in the new bucket
  v_tokens_net  := p_credits;  -- start: assume full grant added to tokens

  FOR v_clawback IN
    SELECT id, amount, ref_type, ref_id
      FROM public.credit_clawbacks
     WHERE user_id = p_user_id
       AND satisfied_at IS NULL
     ORDER BY created_at ASC
     FOR UPDATE
  LOOP
    EXIT WHEN v_available <= 0;

    v_satisfy   := LEAST(v_available, v_clawback.amount);
    v_available := v_available - v_satisfy;

    -- Decrement the new bucket.
    UPDATE public.credit_buckets
       SET remaining_credits = remaining_credits - v_satisfy
     WHERE id = v_bucket_id;

    -- Record a clawback_satisfy transaction for the audit trail.
    SELECT COALESCE(SUM(remaining_credits), 0) INTO v_balance_after
      FROM public.credit_buckets WHERE user_id = p_user_id;

    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'clawback_satisfy', v_satisfy, v_bucket_id,
       v_clawback.ref_type, v_clawback.ref_id,
       v_balance_after,
       jsonb_build_object(
         'source',       'clawback_satisfy',
         'clawback_id',  v_clawback.id,
         'grant_ref_type', p_ref_type,
         'grant_ref_id',   p_ref_id
       ));

    -- Satisfy or partially reduce the clawback row.
    IF v_satisfy >= v_clawback.amount THEN
      UPDATE public.credit_clawbacks
         SET satisfied_at = now()
       WHERE id = v_clawback.id;
    ELSE
      -- Partially satisfied — reduce the remaining amount in-place.
      UPDATE public.credit_clawbacks
         SET amount = amount - v_satisfy
       WHERE id = v_clawback.id;
    END IF;

    -- The tokens net decreases by whatever we satisfied from clawbacks
    -- (those credits went to paying the debt, not the user's spendable balance).
    v_tokens_net := v_tokens_net - v_satisfy;
  END LOOP;

  -- B-05 sync: add only the net credits to profiles.tokens.
  UPDATE public.profiles
     SET tokens = tokens + v_tokens_net,
         updated_at = now()
   WHERE id = p_user_id;

  -- Final ledger balance after all clawback deductions.
  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_balance_after
    FROM public.credit_buckets WHERE user_id = p_user_id;

  RETURN jsonb_build_object(
    'status',         'granted',
    'bucket_id',      v_bucket_id,
    'credits',        p_credits,
    'balance_after',  v_balance_after,
    'clawback_satisfied', (p_credits - v_available)
  );
END;
$$;

-- ============================================================
-- 4. Replace add_credits — net addition against open clawbacks.
--    add_credits is the tokens-only (investigation-flow) path;
--    it does NOT create credit_buckets.  We must still drain
--    newly-added tokens through open clawbacks.
-- ============================================================

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

-- ============================================================
-- 5. Privilege hardening (B-01 / B-02 preserved)
-- ============================================================

REVOKE ALL ON FUNCTION public.refund_credits(uuid, integer, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.add_credits(uuid, integer) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text)                   TO service_role;
GRANT EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz)  TO service_role;
GRANT EXECUTE ON FUNCTION public.add_credits(uuid, integer)                                   TO service_role;

-- credit_clawbacks: service_role bypasses RLS for DML; no explicit grants needed.

-- ============================================================
-- 6. Post-apply invariants
-- ============================================================
DO $post$
DECLARE
  bad text := '';
  fn  text;
  group_a text[] := ARRAY[
    'add_credits','refund_credits','grant_credits'
  ];
  hostile_roles text[] := ARRAY['public','anon','authenticated'];
  rl text;
BEGIN
  FOREACH fn IN ARRAY group_a LOOP
    FOREACH rl IN ARRAY hostile_roles LOOP
      IF EXISTS (
        SELECT 1 FROM information_schema.routine_privileges
        WHERE specific_schema = 'public'
          AND routine_name = fn
          AND grantee = CASE WHEN rl = 'public' THEN 'PUBLIC' ELSE rl END
          AND privilege_type = 'EXECUTE'
      ) THEN
        bad := bad || format('  %s: role %s still has EXECUTE%s', fn, rl, E'\n');
      END IF;
    END LOOP;
  END LOOP;

  IF length(bad) > 0 THEN
    RAISE EXCEPTION '008 FAIL: hostile EXECUTE grants remain:%s%s', E'\n', bad;
  END IF;

  -- credit_clawbacks table must exist.
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'credit_clawbacks'
  ) THEN
    RAISE EXCEPTION '008 FAIL: credit_clawbacks table not created';
  END IF;

  RAISE NOTICE '008 invariants: privilege posture OK, credit_clawbacks table present';
END
$post$;

COMMIT;
