-- =============================================================================
-- Migration 012 — B-16: admin_set_credits writes credit_buckets / credit_transactions ledger
-- =============================================================================
--
-- Problem (B-16): The admin_set_credits RPC updates profiles.tokens directly
-- but never writes a credit_buckets row or a credit_transactions row.  Every
-- call widens the R3 drift (profiles.tokens vs ledger sum).
--
-- Fix chosen: approach (a) — direct set variant.
--   * When credits are ADDED (v_final > v_current): insert a credit_buckets row
--     with source='admin_grant' and a credit_transactions row with type='grant'.
--   * When credits are REMOVED (v_final < v_current): insert a credit_transactions
--     row with type='spend' against the first unexpired bucket (oldest-first FIFO),
--     clamped to what is available.  The amount removed is written even if no
--     bucket exists (e.g. the admin is zero-ing out an already-drifted balance).
--   * When credits are unchanged: no ledger row (idempotent).
--   * profiles.tokens is still updated to v_final (B-05 invariant maintained).
--
-- The credit_transactions type CHECK allows: grant | spend | refund | expiry |
-- clawback_satisfy.  We use 'grant' for additions and 'spend' for subtractions,
-- with metadata={'admin_action': true, 'actor': caller_uuid} to distinguish
-- admin-driven rows from user/stripe-driven rows.
--
-- B-05 invariant (profiles.tokens == SUM credit_buckets.remaining_credits) is
-- maintained because:
--   - additions: new bucket remaining_credits == delta → tokens += delta
--   - subtractions: existing bucket(s) debited by delta → tokens -= delta
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Recreate admin_set_credits with full ledger writes.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.admin_set_credits(
  target_user_id uuid,
  new_credits    integer,
  is_delta       boolean DEFAULT false
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_caller        uuid    := auth.uid();
  v_current       integer;
  v_final         integer;
  v_delta         integer;  -- positive = grant, negative = debit
  v_bucket_id     uuid;
  v_bucket_remain integer;
  v_to_debit      integer;
  v_debited       integer  := 0;
BEGIN
  -- ── Authority check ────────────────────────────────────────────────────────
  IF NOT public.is_admin(v_caller) THEN
    RAISE EXCEPTION 'admin_set_credits: admin access required'
      USING ERRCODE = 'insufficient_privilege';
  END IF;

  -- ── Input validation ───────────────────────────────────────────────────────
  IF NOT is_delta AND new_credits < 0 THEN
    RAISE EXCEPTION 'admin_set_credits: absolute new_credits must be >= 0';
  END IF;

  -- ── Lock row and read current balance ──────────────────────────────────────
  -- B-06 fix already in place: FOR UPDATE serialises concurrent callers.
  SELECT tokens INTO v_current
    FROM public.profiles
   WHERE id = target_user_id
     FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'admin_set_credits: target user not found';
  END IF;

  -- ── Compute final balance ──────────────────────────────────────────────────
  IF is_delta THEN
    v_final := GREATEST(0, v_current + new_credits);
  ELSE
    v_final := new_credits;
  END IF;

  v_delta := v_final - v_current;  -- signed: >0 grant, <0 debit, 0 noop

  -- ── Update profiles.tokens ────────────────────────────────────────────────
  UPDATE public.profiles
     SET tokens     = v_final,
         updated_at = now()
   WHERE id = target_user_id;

  -- ── Ledger writes ─────────────────────────────────────────────────────────
  IF v_delta > 0 THEN
    -- GRANT path: insert a permanent admin_grant bucket + grant transaction.
    INSERT INTO public.credit_buckets (
      user_id, source, original_credits, remaining_credits,
      expires_at, ref_type, ref_id
    ) VALUES (
      target_user_id, 'admin_grant', v_delta, v_delta,
      NULL,  -- permanent grant
      'admin_set_credits', v_caller::text
    )
    RETURNING id INTO v_bucket_id;

    INSERT INTO public.credit_transactions (
      user_id, type, credits, bucket_id,
      ref_type, ref_id, balance_after, metadata
    ) VALUES (
      target_user_id,
      'grant',
      v_delta,
      v_bucket_id,
      'admin_set_credits',
      v_caller::text,
      v_final,
      jsonb_build_object(
        'admin_action', true,
        'actor',        v_caller,
        'mode',         CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END,
        'requested',    new_credits
      )
    );

  ELSIF v_delta < 0 THEN
    -- DEBIT path: drain oldest bucket(s) FIFO, record a spend transaction per bucket.
    v_to_debit := -v_delta;  -- positive amount to remove

    FOR v_bucket_id, v_bucket_remain IN
      SELECT id, remaining_credits
        FROM public.credit_buckets
       WHERE user_id = target_user_id
         AND remaining_credits > 0
         AND (expires_at IS NULL OR expires_at > now())
       ORDER BY granted_at ASC
         FOR UPDATE SKIP LOCKED
    LOOP
      EXIT WHEN v_to_debit <= 0;

      v_debited := LEAST(v_bucket_remain, v_to_debit);

      UPDATE public.credit_buckets
         SET remaining_credits = remaining_credits - v_debited
       WHERE id = v_bucket_id;

      INSERT INTO public.credit_transactions (
        user_id, type, credits, bucket_id,
        ref_type, ref_id, balance_after, metadata
      ) VALUES (
        target_user_id,
        'spend',
        v_debited,
        v_bucket_id,
        'admin_set_credits',
        v_caller::text,
        v_final,
        jsonb_build_object(
          'admin_action', true,
          'actor',        v_caller,
          'mode',         CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END,
          'requested',    new_credits
        )
      );

      v_to_debit := v_to_debit - v_debited;
    END LOOP;

    -- If no (or insufficient) buckets existed, record the remainder as a
    -- synthetic spend against a NULL bucket so the audit trail is complete.
    IF v_to_debit > 0 THEN
      INSERT INTO public.credit_transactions (
        user_id, type, credits, bucket_id,
        ref_type, ref_id, balance_after, metadata
      ) VALUES (
        target_user_id,
        'spend',
        v_to_debit,
        NULL,
        'admin_set_credits',
        v_caller::text,
        v_final,
        jsonb_build_object(
          'admin_action',        true,
          'actor',               v_caller,
          'mode',                CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END,
          'requested',           new_credits,
          'no_bucket_available', true
        )
      );
    END IF;
  END IF;
  -- v_delta == 0: no ledger row needed (balance unchanged).

  -- ── Audit (already present from 004/007 — preserved) ──────────────────────
  PERFORM public.admin_audit_insert(
    p_actor_id   := v_caller,
    p_action     := 'admin.set_credits',
    p_target_type:= 'user',
    p_target_id  := target_user_id::text,
    p_before     := jsonb_build_object('tokens', v_current),
    p_after      := jsonb_build_object('tokens', v_final),
    p_metadata   := jsonb_build_object(
                      'mode',   CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END,
                      'amount', new_credits,
                      'delta',  v_delta
                    ),
    p_ip         := NULL,
    p_user_agent := NULL
  );

  RETURN v_final;
END;
$$;

-- Grants: preserve exact same permissions as 004/007.
REVOKE ALL ON FUNCTION public.admin_set_credits(uuid, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean) TO authenticated;

-- -----------------------------------------------------------------------------
-- 2. Smoke-test: verify the body references credit_transactions (invariant check).
-- -----------------------------------------------------------------------------
DO $$
DECLARE
  fn_body text;
BEGIN
  SELECT prosrc INTO fn_body
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'public' AND p.proname = 'admin_set_credits';

  IF fn_body NOT ILIKE '%credit_transactions%' THEN
    RAISE EXCEPTION '012 invariant FAIL: admin_set_credits body does not reference credit_transactions';
  END IF;

  IF fn_body NOT ILIKE '%credit_buckets%' THEN
    RAISE EXCEPTION '012 invariant FAIL: admin_set_credits body does not reference credit_buckets';
  END IF;
END $$;

COMMIT;
