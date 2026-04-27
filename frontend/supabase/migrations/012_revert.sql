-- =============================================================================
-- Migration 012 REVERT — restore admin_set_credits to 007 version (no ledger)
-- =============================================================================
--
-- WARNING: This revert removes the credit_transactions / credit_buckets writes
-- from admin_set_credits, restoring the 007 behaviour (profiles.tokens only).
-- Any ledger rows written by the 012 version since deployment are NOT removed
-- (they are correct historical data and should be kept).
--
-- This revert is provided for emergency rollback only.

BEGIN;

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
     SET tokens     = v_final,
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

REVOKE ALL ON FUNCTION public.admin_set_credits(uuid, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean) TO authenticated;

COMMIT;
