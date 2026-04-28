--
-- PostgreSQL database dump
--


-- Dumped from database version 17.9 (Debian 17.9-0+deb13u1)
-- Dumped by pg_dump version 17.9 (Debian 17.9-0+deb13u1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: auth; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS auth;


--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: role(); Type: FUNCTION; Schema: auth; Owner: -
--

CREATE FUNCTION auth.role() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT COALESCE(current_setting('request.jwt.claim.role', true), 'anon') $$;


--
-- Name: uid(); Type: FUNCTION; Schema: auth; Owner: -
--

CREATE FUNCTION auth.uid() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true),'')::uuid $$;


--
-- Name: add_credits(uuid, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.add_credits(p_user_id uuid, p_credits integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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


--
-- Name: admin_adjust_credits(uuid, uuid, text, integer, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.admin_adjust_credits(p_caller uuid, p_target uuid, p_mode text, p_amount integer, p_reason text DEFAULT NULL::text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'auth'
    AS $$
DECLARE v_old INTEGER; v_new INTEGER;
BEGIN
  IF NOT public.is_admin(p_caller) THEN RAISE EXCEPTION 'not_admin'; END IF;
  IF p_mode NOT IN ('set','delta') THEN RAISE EXCEPTION 'invalid_mode'; END IF;
  SELECT tokens INTO v_old FROM public.profiles WHERE id = p_target FOR UPDATE;
  IF p_mode = 'set' THEN v_new := GREATEST(p_amount, 0);
  ELSE v_new := GREATEST(COALESCE(v_old,0) + p_amount, 0); END IF;
  UPDATE public.profiles SET tokens = v_new, updated_at = NOW() WHERE id = p_target;
  PERFORM public.admin_audit_insert(p_caller, 'credits.adjust', 'user', p_target::text,
    jsonb_build_object('tokens', v_old),
    jsonb_build_object('tokens', v_new, 'mode', p_mode, 'amount', p_amount, 'reason', p_reason));
  RETURN v_new;
END;
$$;


--
-- Name: admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.admin_audit_insert(p_actor_id uuid, p_action text, p_target_type text DEFAULT NULL::text, p_target_id text DEFAULT NULL::text, p_before jsonb DEFAULT NULL::jsonb, p_after jsonb DEFAULT NULL::jsonb, p_metadata jsonb DEFAULT NULL::jsonb, p_ip text DEFAULT NULL::text, p_user_agent text DEFAULT NULL::text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_id    uuid;
  v_email text;
BEGIN
  -- Require caller to be an admin
  IF NOT public.is_admin(auth.uid()) THEN
    RAISE EXCEPTION 'permission denied' USING ERRCODE = '42501';
  END IF;
  -- Prevent replay: actor_id must match the calling user
  IF auth.uid() IS NOT NULL AND auth.uid() <> p_actor_id THEN
    RAISE EXCEPTION 'permission denied: actor_id mismatch' USING ERRCODE = '42501';
  END IF;
  SELECT email INTO v_email FROM public.profiles WHERE id = p_actor_id;
  INSERT INTO public.audit_log (actor_id, actor_email, action, target_type, target_id,
                                before, after, metadata, ip_address, user_agent)
  VALUES (p_actor_id, v_email, p_action, p_target_type, p_target_id,
          p_before, p_after, p_metadata, p_ip, p_user_agent)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;


--
-- Name: admin_count_profiles(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.admin_count_profiles() RETURNS integer
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
  IF NOT public.is_admin(auth.uid()) THEN
    RAISE EXCEPTION 'permission denied' USING ERRCODE = '42501';
  END IF;
  RETURN (SELECT COUNT(*)::integer FROM public.profiles);
END;
$$;


--
-- Name: admin_list_profiles(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.admin_list_profiles() RETURNS TABLE(id uuid, email text, full_name text, tokens integer, role text, stripe_customer_id text, subscription_plan text, subscription_status text, created_at timestamp with time zone)
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
  IF NOT public.is_admin(auth.uid()) THEN
    RAISE EXCEPTION 'permission denied' USING ERRCODE = '42501';
  END IF;
  RETURN QUERY
    SELECT p.id, p.email, p.full_name, p.tokens, p.role,
           p.stripe_customer_id, p.subscription_plan, p.subscription_status, p.created_at
      FROM public.profiles p
     ORDER BY p.created_at DESC;
END;
$$;


--
-- Name: admin_set_credits(uuid, integer, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.admin_set_credits(target_user_id uuid, new_credits integer, is_delta boolean DEFAULT false) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
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


--
-- Name: check_balance(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.check_balance(target_user_id uuid) RETURNS integer
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
  SELECT tokens FROM public.profiles WHERE id = target_user_id;
$$;


--
-- Name: check_profile_immutable(uuid, text, text, integer, text, text, text, text, timestamp with time zone, timestamp with time zone, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.check_profile_immutable(p_id uuid, p_role text, p_plan text, p_tokens integer, p_stripe_customer_id text, p_stripe_subscription_id text, p_subscription_status text, p_subscription_plan text, p_subscription_current_period_end timestamp with time zone, p_suspended_at timestamp with time zone, p_suspended_reason text, p_admin_notes text) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
  -- Returns true iff all admin-locked fields in the current DB row match
  -- the supplied (NEW) values. If the row doesn't exist, returns NULL
  -- (treated as false by WITH CHECK).
  SELECT
    COALESCE(p.role = p_role, true)
    AND COALESCE(p.plan = p_plan, true)
    AND COALESCE(p.tokens = p_tokens, true)
    AND COALESCE(p.stripe_customer_id, '') = COALESCE(p_stripe_customer_id, '')
    AND COALESCE(p.stripe_subscription_id, '') = COALESCE(p_stripe_subscription_id, '')
    AND COALESCE(p.subscription_status, 'none') = COALESCE(p_subscription_status, 'none')
    AND COALESCE(p.subscription_plan, 'none') = COALESCE(p_subscription_plan, 'none')
    AND COALESCE(p.subscription_current_period_end::text, '') = COALESCE(p_subscription_current_period_end::text, '')
    AND COALESCE(p.suspended_at::text, '') = COALESCE(p_suspended_at::text, '')
    AND COALESCE(p.suspended_reason, '') = COALESCE(p_suspended_reason, '')
    AND COALESCE(p.admin_notes, '') = COALESCE(p_admin_notes, '')
  FROM public.profiles p
  WHERE p.id = p_id;
$$;


--
-- Name: deduct_credits(uuid, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.deduct_credits(target_user_id uuid, amount integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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


--
-- Name: expire_credits(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.expire_credits() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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


--
-- Name: get_stripe_customer_id(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_stripe_customer_id(target_user_id uuid) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE result text;
BEGIN
  SELECT stripe_customer_id INTO result FROM public.profiles WHERE id = target_user_id;
  RETURN result;
END;
$$;


--
-- Name: get_user_tokens(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_user_tokens(target_user_id uuid) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE result integer;
BEGIN
  SELECT tokens INTO result FROM public.profiles WHERE id = target_user_id;
  RETURN COALESCE(result, 0);
END;
$$;


--
-- Name: grant_credits(uuid, integer, text, text, text, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.grant_credits(p_user_id uuid, p_credits integer, p_source text, p_ref_type text DEFAULT NULL::text, p_ref_id text DEFAULT NULL::text, p_expires_at timestamp with time zone DEFAULT NULL::timestamp with time zone) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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


--
-- Name: handle_new_user(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.handle_new_user() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
/*
  B-14 fix — atomic signup trigger.

  Design decisions:
  1. Profiles INSERT uses ON CONFLICT (id) DO NOTHING — trigger is idempotent
     (safe to replay if Supabase retries).
  2. Credit bucket INSERT is wrapped in a nested BEGIN/EXCEPTION block
     (savepoint semantics via PL/pgSQL). If the bucket grant fails, we log
     a NOTICE and RE-RAISE so the whole transaction rolls back — atomicity
     is preserved and the auth.users INSERT also rolls back.
  3. Any unexpected exception from the profiles INSERT is re-raised so that
     Postgres rolls back the auth.users row (AFTER trigger, same transaction).
*/
BEGIN
  BEGIN
    -- Insert profile (idempotent)
    INSERT INTO public.profiles (id, email, full_name)
    VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name')
    ON CONFLICT (id) DO NOTHING;

    -- Insert initial credit bucket (nested sub-transaction)
    BEGIN
      INSERT INTO public.credit_buckets (user_id, source, original_credits, remaining_credits)
      VALUES (NEW.id, 'signup_grant', 500, 500);
    EXCEPTION WHEN OTHERS THEN
      -- Credit bucket failure is logged and re-raised to roll back everything.
      -- Rationale: atomicity — a user without a credit bucket is in an
      -- inconsistent state. Rolling back auth.users is safer than a phantom.
      RAISE NOTICE 'handle_new_user: credit_buckets INSERT failed for user %: % (SQLSTATE: %)',
        NEW.id, SQLERRM, SQLSTATE;
      RAISE;
    END;

  EXCEPTION WHEN OTHERS THEN
    -- Re-raise so Postgres rolls back the auth.users INSERT too.
    RAISE NOTICE 'handle_new_user: unexpected error for user %: % (SQLSTATE: %)',
      NEW.id, SQLERRM, SQLSTATE;
    RAISE;
  END;

  RETURN NEW;
END;
$$;


--
-- Name: is_admin(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.is_admin(user_id uuid) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'auth'
    AS $$
  SELECT COALESCE((SELECT role = 'admin' FROM public.profiles WHERE id = user_id), false);
$$;


--
-- Name: process_charge_reversal(uuid, text, text, text, text, integer, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.process_charge_reversal(p_user_id uuid, p_charge_id text, p_dispute_id text, p_payment_intent_id text, p_reversal_key text, p_target_credits integer, p_first_event_id text, p_first_event_type text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_already_reversed integer;
  v_incremental      integer;
  v_inserted_count   integer := 0;
  v_refund_result    jsonb;
BEGIN
  -- Validate inputs.
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'process_charge_reversal: p_user_id required';
  END IF;
  IF p_charge_id IS NULL OR length(p_charge_id) = 0 THEN
    RAISE EXCEPTION 'process_charge_reversal: p_charge_id required';
  END IF;
  IF p_reversal_key IS NULL OR length(p_reversal_key) = 0 THEN
    RAISE EXCEPTION 'process_charge_reversal: p_reversal_key required';
  END IF;
  IF p_target_credits IS NULL OR p_target_credits < 0 THEN
    RAISE EXCEPTION 'process_charge_reversal: p_target_credits must be >= 0';
  END IF;

  -- K-02 fix: per-charge serialization. Held until transaction commit.
  -- Distinct from the per-user lock taken inside refund_credits (different
  -- key shape, no collision). Two concurrent calls on the same charge
  -- serialize here; the second waits until the first commits its dedup
  -- INSERT, then computes the correct incremental delta.
  PERFORM pg_advisory_xact_lock(hashtextextended('charge:' || p_charge_id, 0));

  -- Dedup: if a row already exists for this exact reversal_key (e.g., a
  -- replay of the same webhook event_id), short-circuit.
  IF EXISTS (
    SELECT 1 FROM public.stripe_dispute_reversals
     WHERE reversal_key = p_reversal_key
  ) THEN
    RETURN jsonb_build_object('status', 'duplicate', 'credits', 0);
  END IF;

  -- Sum credits already reversed for this charge across all reversal_keys
  -- (distinct refund events, prior dispute events, etc.). Now safe because
  -- any concurrent process_charge_reversal call for the same charge is
  -- waiting on the per-charge lock above.
  SELECT COALESCE(SUM(credits), 0) INTO v_already_reversed
    FROM public.stripe_dispute_reversals
   WHERE charge_id = p_charge_id;

  v_incremental := GREATEST(0, p_target_credits - v_already_reversed);

  -- Always insert the dedup row first so that any future call for the same
  -- reversal_key (whether incremental was 0 or positive) short-circuits.
  -- The unique constraint on reversal_key collapses concurrent retries on
  -- the same key — but the per-charge advisory lock above already prevents
  -- that interleave.
  INSERT INTO public.stripe_dispute_reversals
    (reversal_key, user_id, charge_id, dispute_id, payment_intent_id,
     credits, first_event_id, first_event_type)
  VALUES
    (p_reversal_key, p_user_id, p_charge_id, p_dispute_id, p_payment_intent_id,
     v_incremental, p_first_event_id, p_first_event_type)
  ON CONFLICT (reversal_key) DO NOTHING;
  GET DIAGNOSTICS v_inserted_count = ROW_COUNT;

  IF v_inserted_count = 0 THEN
    -- Another caller raced past the EXISTS check (only possible if the
    -- per-charge lock did not actually serialize them — should never
    -- happen, but treat as duplicate defensively).
    RETURN jsonb_build_object('status', 'duplicate', 'credits', 0);
  END IF;

  IF v_incremental <= 0 THEN
    RETURN jsonb_build_object(
      'status', 'already_satisfied',
      'credits', 0,
      'already_reversed', v_already_reversed
    );
  END IF;

  -- Call refund_credits in the SAME transaction. refund_credits acquires
  -- pg_advisory_xact_lock on the user — that lock is acquired AFTER our
  -- per-charge lock, so the lock order is: charge → user. No other code
  -- path takes user before charge, so no deadlock cycle.
  -- refund_credits also dedups on (type='refund', ref_type, ref_id) — we
  -- pass ref_id = p_reversal_key for that purpose.
  v_refund_result := public.refund_credits(
    p_user_id   := p_user_id,
    p_credits   := v_incremental,
    p_ref_type  := 'stripe_event',
    p_ref_id    := p_reversal_key
  );

  RETURN jsonb_build_object(
    'status',           'reversed',
    'credits',          v_incremental,
    'already_reversed', v_already_reversed,
    'refund_result',    v_refund_result
  );
END;
$$;


--
-- Name: refund_credits(uuid, integer, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.refund_credits(p_user_id uuid, p_credits integer, p_ref_type text, p_ref_id text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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
  v_first_bucket   uuid;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN
    RAISE EXCEPTION 'refund_credits: p_credits must be > 0';
  END IF;
  IF p_user_id IS NULL THEN
    RAISE EXCEPTION 'refund_credits: p_user_id required';
  END IF;

  -- Per-user serialization (same lock key as grant/spend).
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  -- Idempotency: check credit_transactions and credit_clawbacks.
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

  -- BB-01 fix: FIFO debit loop NO LONGER inserts per-bucket
  -- credit_transactions rows.  The per-bucket movement is recorded in
  -- credit_buckets.remaining_credits; the aggregate ledger row is
  -- inserted ONCE after the loop so it matches the uq_credit_tx_idem
  -- dedup contract.
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

    IF v_first_bucket IS NULL THEN
      v_first_bucket := v_bucket.id;
    END IF;

    v_remaining := v_remaining - v_take;
  END LOOP;

  -- Compute final balance after all bucket debits.
  SELECT COALESCE(SUM(remaining_credits), 0) INTO v_balance_after
    FROM public.credit_buckets
   WHERE user_id = p_user_id;

  IF v_balance_after IS NULL THEN
    v_balance_after := 0;
  END IF;

  -- BB-01: aggregate ledger row.  Inserted only when actual credits
  -- were debited; deficit-only refunds (balance was 0) skip this and
  -- record a clawback row below — same as the prior contract.
  -- The credits column has CHECK (credits > 0), so we only INSERT when
  -- v_to_debit_now > 0.
  IF v_to_debit_now > 0 THEN
    INSERT INTO public.credit_transactions
      (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES
      (p_user_id, 'refund', v_to_debit_now, v_first_bucket, p_ref_type, p_ref_id,
       v_balance_after,
       jsonb_build_object(
         'source', 'stripe_refund',
         'deficit', v_deficit,
         'aggregate', true
       ));
  END IF;

  -- Record any unsatisfied portion as a clawback row.
  -- This is the F-03 contract preserved: we do not silently forgive
  -- the deficit.
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


--
-- Name: spend_credits(uuid, integer, text, text, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.spend_credits(p_user_id uuid, p_credits integer, p_ref_type text, p_ref_id text, p_metadata jsonb DEFAULT '{}'::jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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


--
-- Name: update_profile_by_id(uuid, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_profile_by_id(target_user_id uuid, payload jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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


--
-- Name: update_profile_by_stripe_customer(text, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_profile_by_stripe_customer(target_customer_id text, payload jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
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
         -- F-04: sync plan from webhook payload so enforcement stays consistent.
         plan       = COALESCE(payload->>'plan', plan),
         updated_at = now()
   WHERE stripe_customer_id = target_customer_id;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: users; Type: TABLE; Schema: auth; Owner: -
--

CREATE TABLE auth.users (
    id uuid NOT NULL,
    email text,
    raw_user_meta_data jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: agent_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_events (
    id bigint NOT NULL,
    task_id uuid NOT NULL,
    event_type text NOT NULL,
    state text,
    step_id text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_events_id_seq OWNED BY public.agent_events.id;


--
-- Name: agent_settlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_settlements (
    task_id uuid NOT NULL,
    user_id text NOT NULL,
    reserved_credits bigint NOT NULL,
    final_credits bigint NOT NULL,
    delta_credits bigint NOT NULL,
    ref_id text NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    ledger_applied_at timestamp with time zone,
    CONSTRAINT agent_settlements_final_credits_check CHECK ((final_credits >= 0)),
    CONSTRAINT agent_settlements_reserved_credits_check CHECK ((reserved_credits >= 0))
);


--
-- Name: agent_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_tasks (
    id uuid NOT NULL,
    user_id text NOT NULL,
    conversation_id text,
    goal text NOT NULL,
    user_instructions text,
    state text NOT NULL,
    selected_model text DEFAULT 'claude-opus-4-7-20260208'::text NOT NULL,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    artifacts jsonb DEFAULT '[]'::jsonb NOT NULL,
    max_duration_hours double precision DEFAULT 2.0 NOT NULL,
    budget_usd double precision DEFAULT 5.0 NOT NULL,
    spent_usd double precision DEFAULT 0.0 NOT NULL,
    max_fix_attempts_per_step integer DEFAULT 5 NOT NULL,
    max_replans integer DEFAULT 3 NOT NULL,
    replan_count integer DEFAULT 0 NOT NULL,
    total_failures integer DEFAULT 0 NOT NULL,
    final_answer text,
    stop_requested boolean DEFAULT false NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    reserved_credits bigint DEFAULT 0 NOT NULL,
    credits_settled boolean DEFAULT false NOT NULL,
    requires_vault boolean DEFAULT false NOT NULL
);


--
-- Name: ai_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_sessions (
    id text NOT NULL,
    task_id text NOT NULL,
    branch_id text,
    task_type text NOT NULL,
    model_used text NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cache_creation_tokens integer DEFAULT 0 NOT NULL,
    cache_read_tokens integer DEFAULT 0 NOT NULL,
    cost_usd numeric DEFAULT 0 NOT NULL,
    duration_ms integer DEFAULT 0 NOT NULL,
    used_batch_api boolean DEFAULT false NOT NULL,
    batch_id text,
    cache_hit boolean DEFAULT false NOT NULL,
    started_at timestamp with time zone NOT NULL,
    error text
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    actor_id uuid,
    actor_email text,
    action text NOT NULL,
    target_type text,
    target_id text,
    before jsonb,
    after jsonb,
    metadata jsonb,
    ip_address text,
    user_agent text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: audit_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_results (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    audit_type text DEFAULT 'full'::text NOT NULL,
    issues jsonb DEFAULT '[]'::jsonb NOT NULL,
    passed boolean DEFAULT false NOT NULL,
    overall_score double precision DEFAULT 0.0 NOT NULL,
    auditor_notes text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: branches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.branches (
    id text NOT NULL,
    hypothesis_id text NOT NULL,
    task_id text NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    score_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    budget_allocated numeric DEFAULT 5.0 NOT NULL,
    budget_spent numeric DEFAULT 0 NOT NULL,
    grants_log jsonb DEFAULT '[]'::jsonb NOT NULL,
    cycles_completed integer DEFAULT 0 NOT NULL,
    kill_reason text,
    sources_searched jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoints (
    id text NOT NULL,
    task_id text NOT NULL,
    "timestamp" timestamp with time zone NOT NULL,
    state_machine_state text NOT NULL,
    active_branch_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    killed_branch_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    compressed_findings jsonb DEFAULT '[]'::jsonb NOT NULL,
    budget_remaining numeric NOT NULL,
    total_spent numeric NOT NULL,
    diminishing_flags integer DEFAULT 0 NOT NULL,
    ai_call_counter integer DEFAULT 0 NOT NULL,
    snapshot_path text,
    diminishing_result text
);


--
-- Name: claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.claims (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    finding_id text,
    hypothesis_id text,
    subject text NOT NULL,
    predicate text NOT NULL,
    object text NOT NULL,
    claim_text text NOT NULL,
    source_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence double precision DEFAULT 0.5 NOT NULL,
    credibility_score double precision,
    corroboration_count integer DEFAULT 0 NOT NULL,
    contradiction_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    temporal_start timestamp with time zone,
    temporal_end timestamp with time zone,
    temporal_type text DEFAULT 'point'::text,
    is_resolved boolean DEFAULT false NOT NULL,
    resolution_note text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: contradiction_pairs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contradiction_pairs (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    claim_a_id text NOT NULL,
    claim_b_id text NOT NULL,
    contradiction_type text DEFAULT 'direct'::text NOT NULL,
    severity double precision DEFAULT 0.5 NOT NULL,
    resolution_status text DEFAULT 'unresolved'::text NOT NULL,
    resolution_source_id text,
    resolution_note text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    title text DEFAULT 'New conversation'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: credit_buckets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_buckets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    source text NOT NULL,
    original_credits integer NOT NULL,
    remaining_credits integer NOT NULL,
    granted_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    expires_at timestamp with time zone,
    ref_type text,
    ref_id text,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT credit_buckets_source_check CHECK ((source = ANY (ARRAY['signup_grant'::text, 'plan_renewal'::text, 'topup'::text, 'admin_grant'::text, 'refund'::text])))
);


--
-- Name: credit_clawbacks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_clawbacks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    amount integer NOT NULL,
    ref_type text NOT NULL,
    ref_id text NOT NULL,
    satisfied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credit_clawbacks_amount_check CHECK ((amount > 0))
);


--
-- Name: credit_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    type text NOT NULL,
    credits integer NOT NULL,
    bucket_id uuid,
    ref_type text,
    ref_id text,
    balance_after integer NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT credit_transactions_credits_check CHECK ((credits > 0)),
    CONSTRAINT credit_transactions_type_check CHECK ((type = ANY (ARRAY['grant'::text, 'spend'::text, 'refund'::text, 'expiry'::text, 'clawback_satisfy'::text])))
);


--
-- Name: evaluation_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evaluation_results (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    branch_id text NOT NULL,
    score numeric NOT NULL,
    reasoning text,
    next_search_keywords jsonb DEFAULT '[]'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: executive_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.executive_summaries (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    one_liner text DEFAULT ''::text NOT NULL,
    paragraph text DEFAULT ''::text NOT NULL,
    page_summary text DEFAULT ''::text NOT NULL,
    full_summary text DEFAULT ''::text NOT NULL,
    compression_metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.findings (
    id text NOT NULL,
    task_id text NOT NULL,
    hypothesis_id text NOT NULL,
    content text NOT NULL,
    content_en text,
    content_language text DEFAULT 'en'::text NOT NULL,
    source_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence numeric DEFAULT 0.5 NOT NULL,
    evidence_type text DEFAULT 'NEUTRAL'::text NOT NULL,
    is_compressed boolean DEFAULT false NOT NULL,
    raw_content_path text,
    created_at timestamp with time zone NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: gap_analyses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gap_analyses (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    gaps jsonb DEFAULT '[]'::jsonb NOT NULL,
    follow_ups_launched jsonb DEFAULT '[]'::jsonb NOT NULL,
    analysis_round integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: graph_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.graph_edges (
    id text NOT NULL,
    task_id text NOT NULL,
    source_node text NOT NULL,
    target_node text NOT NULL,
    label text DEFAULT ''::text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    source text DEFAULT 'ai'::text
);


--
-- Name: graph_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.graph_nodes (
    id text NOT NULL,
    task_id text NOT NULL,
    label text NOT NULL,
    type text DEFAULT 'entity'::text NOT NULL,
    description text DEFAULT ''::text,
    metadata jsonb DEFAULT '{}'::jsonb,
    x double precision,
    y double precision,
    created_at timestamp with time zone DEFAULT now(),
    source text DEFAULT 'ai'::text
);


--
-- Name: hypotheses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hypotheses (
    id text NOT NULL,
    task_id text NOT NULL,
    parent_id text,
    depth integer DEFAULT 0 NOT NULL,
    statement text NOT NULL,
    status text DEFAULT 'PENDING'::text NOT NULL,
    score numeric,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: hypothesis_priors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hypothesis_priors (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    hypothesis_id text NOT NULL,
    prior double precision DEFAULT 0.5 NOT NULL,
    posterior double precision DEFAULT 0.5 NOT NULL,
    evidence_updates jsonb DEFAULT '[]'::jsonb NOT NULL,
    last_updated timestamp with time zone DEFAULT now()
);


--
-- Name: investigation_outcomes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.investigation_outcomes (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text,
    user_id text NOT NULL,
    topic text NOT NULL,
    quality_tier text,
    total_cost_usd double precision DEFAULT 0.0,
    total_ai_calls integer DEFAULT 0,
    duration_seconds integer DEFAULT 0,
    final_state text,
    report_generated boolean DEFAULT false,
    user_rating integer,
    user_feedback text,
    hypotheses_count integer DEFAULT 0,
    findings_count integer DEFAULT 0,
    killed_branches_count integer DEFAULT 0,
    tribunal_verdicts jsonb DEFAULT '[]'::jsonb,
    skeptic_pass boolean,
    patterns jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: investigations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.investigations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    ticker text DEFAULT ''::text,
    hypothesis text DEFAULT ''::text,
    status text DEFAULT 'PENDING'::text NOT NULL,
    depth text DEFAULT 'deep'::text NOT NULL,
    model text DEFAULT 'fast'::text NOT NULL,
    budget_usd numeric DEFAULT 50.00 NOT NULL,
    backend_investigation_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    task_id text,
    topic text,
    duration_hours double precision DEFAULT 2.0,
    output_pdf_path text,
    output_docx_path text,
    conversation_id uuid
);


--
-- Name: learning_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.learning_events (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    user_id text NOT NULL,
    task_id text,
    event_type text NOT NULL,
    category text,
    content jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: learning_insights; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.learning_insights (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    user_id text NOT NULL,
    insight_type text NOT NULL,
    insight_key text NOT NULL,
    insight_value jsonb DEFAULT '{}'::jsonb NOT NULL,
    confidence double precision DEFAULT 0.5,
    sample_count integer DEFAULT 1,
    last_updated timestamp with time zone DEFAULT now()
);


--
-- Name: loop6_007_applied; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.loop6_007_applied (
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    one_row boolean DEFAULT true NOT NULL,
    CONSTRAINT loop6_007_applied_one_row_check CHECK ((one_row = true))
);


--
-- Name: loop6_008_applied; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.loop6_008_applied (
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    label text DEFAULT 'f04_plan_entitlement_sync'::text NOT NULL
);


--
-- Name: orchestrator_handoffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orchestrator_handoffs (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    phase text NOT NULL,
    context jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: perspective_syntheses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.perspective_syntheses (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    perspective text NOT NULL,
    synthesis_text text NOT NULL,
    key_arguments jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence double precision DEFAULT 0.5 NOT NULL,
    cited_claim_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.profiles (
    id uuid NOT NULL,
    email text NOT NULL,
    full_name text,
    tokens integer DEFAULT 500 NOT NULL,
    plan text DEFAULT 'flagship'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    role text DEFAULT 'user'::text NOT NULL,
    stripe_customer_id text,
    stripe_subscription_id text,
    subscription_status text DEFAULT 'none'::text,
    subscription_plan text DEFAULT 'none'::text,
    subscription_current_period_end timestamp with time zone,
    suspended_at timestamp with time zone,
    suspended_reason text,
    admin_notes text,
    CONSTRAINT profiles_role_check CHECK ((role = ANY (ARRAY['user'::text, 'admin'::text, 'banned'::text])))
);


--
-- Name: report_generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.report_generations (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    pdf_path text,
    docx_path text,
    report_cost_usd numeric DEFAULT 0 NOT NULL,
    generated_at timestamp with time zone NOT NULL
);


--
-- Name: research_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.research_plans (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    plan_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    trigger_reason text,
    spawned_branches jsonb DEFAULT '[]'::jsonb,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: research_settlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.research_settlements (
    task_id text NOT NULL,
    user_id text NOT NULL,
    reserved_credits bigint NOT NULL,
    final_credits bigint NOT NULL,
    delta_credits bigint NOT NULL,
    ref_id text NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL,
    ledger_applied_at timestamp with time zone,
    completed_at timestamp with time zone,
    CONSTRAINT research_settlements_final_credits_check CHECK ((final_credits >= 0)),
    CONSTRAINT research_settlements_reserved_credits_check CHECK ((reserved_credits >= 0))
);


--
-- Name: research_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.research_tasks (
    id text NOT NULL,
    topic text NOT NULL,
    budget_usd numeric NOT NULL,
    status text DEFAULT 'PENDING'::text NOT NULL,
    current_state text DEFAULT 'INIT'::text NOT NULL,
    total_spent_usd numeric DEFAULT 0 NOT NULL,
    diminishing_flags integer DEFAULT 0 NOT NULL,
    ai_call_counter integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    error_message text,
    output_pdf_path text,
    output_docx_path text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    quality_tier text DEFAULT 'balanced'::text,
    user_flow_instructions text DEFAULT ''::text,
    continuous_mode boolean DEFAULT false,
    dont_kill_branches boolean DEFAULT false,
    user_id uuid,
    credits_settled boolean DEFAULT false NOT NULL
);


--
-- Name: skeptic_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skeptic_results (
    id text NOT NULL,
    task_id text NOT NULL,
    finding_id text NOT NULL,
    tribunal_session_id text,
    questions jsonb DEFAULT '[]'::jsonb NOT NULL,
    open_count integer DEFAULT 0 NOT NULL,
    researchable_count integer DEFAULT 0 NOT NULL,
    resolved_count integer DEFAULT 0 NOT NULL,
    critical_open_count integer DEFAULT 0 NOT NULL,
    passes_publishing_threshold boolean DEFAULT false NOT NULL,
    cost_usd numeric DEFAULT 0 NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: source_scores; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source_scores (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    source_id text NOT NULL,
    task_id text NOT NULL,
    domain text NOT NULL,
    credibility double precision DEFAULT 0.5 NOT NULL,
    relevance double precision DEFAULT 0.5 NOT NULL,
    recency double precision DEFAULT 0.5 NOT NULL,
    composite_score double precision DEFAULT 0.5 NOT NULL,
    domain_authority text DEFAULT 'unknown'::text,
    publication_type text DEFAULT 'unknown'::text,
    cross_ref_density integer DEFAULT 0 NOT NULL,
    scoring_rationale text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sources (
    id text NOT NULL,
    task_id text NOT NULL,
    url text NOT NULL,
    url_hash text NOT NULL,
    title text,
    title_en text,
    content_hash text,
    fetched_at timestamp with time zone NOT NULL,
    cache_expiry timestamp with time zone,
    source_type text DEFAULT 'NEWS'::text NOT NULL,
    language text DEFAULT 'en'::text NOT NULL,
    adapter_name text,
    is_paywalled boolean DEFAULT false NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: stripe_dispute_reversals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stripe_dispute_reversals (
    reversal_key text NOT NULL,
    user_id uuid NOT NULL,
    charge_id text,
    dispute_id text,
    payment_intent_id text,
    credits integer NOT NULL,
    first_event_id text NOT NULL,
    first_event_type text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: stripe_payment_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stripe_payment_grants (
    payment_intent_id text NOT NULL,
    charge_id text,
    user_id uuid NOT NULL,
    credits integer NOT NULL,
    event_id text NOT NULL,
    source text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    charge_amount integer,
    CONSTRAINT stripe_payment_grants_charge_amount_check CHECK (((charge_amount IS NULL) OR (charge_amount > 0))),
    CONSTRAINT stripe_payment_grants_credits_check CHECK ((credits > 0))
);


--
-- Name: COLUMN stripe_payment_grants.charge_amount; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.stripe_payment_grants.charge_amount IS 'Original Stripe charge.amount in cents at grant time. K-01: used by _reverse_credits_for_charge as amount_total when computing pro-rata reversal for partial disputes. NULL on legacy rows (best-effort backfill not feasible without re-fetching Stripe charges); the reversal flow falls back to the pre-K-01 behaviour and logs a warning when this column is NULL.';


--
-- Name: stripe_pending_reversals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stripe_pending_reversals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id text NOT NULL,
    charge_id text,
    payment_intent_id text,
    kind text NOT NULL,
    amount_cents bigint NOT NULL,
    currency text NOT NULL,
    raw_event jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_at timestamp with time zone,
    CONSTRAINT stripe_pending_reversals_amount_cents_check CHECK ((amount_cents >= 0)),
    CONSTRAINT stripe_pending_reversals_check CHECK (((charge_id IS NOT NULL) OR (payment_intent_id IS NOT NULL))),
    CONSTRAINT stripe_pending_reversals_kind_check CHECK ((kind = ANY (ARRAY['refund'::text, 'dispute_created'::text, 'dispute_funds_withdrawn'::text])))
);


--
-- Name: TABLE stripe_pending_reversals; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.stripe_pending_reversals IS 'U-01: parking lot for Stripe charge.refunded / charge.dispute.* events that arrived before the corresponding stripe_payment_grants row existed. Reconciled on grant insert.';


--
-- Name: stripe_webhook_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stripe_webhook_events (
    event_id text NOT NULL,
    event_type text NOT NULL,
    status text DEFAULT 'completed'::text NOT NULL,
    attempts integer DEFAULT 1 NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    last_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    last_error text,
    processed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT stripe_webhook_events_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'completed'::text])))
);


--
-- Name: system_status; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_status (
    id integer DEFAULT 1 NOT NULL,
    frozen boolean DEFAULT false NOT NULL,
    frozen_reason text,
    frozen_by uuid,
    frozen_at timestamp with time zone,
    maintenance_message text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tribunal_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tribunal_sessions (
    id text NOT NULL,
    task_id text NOT NULL,
    finding_id text NOT NULL,
    plaintiff_args text,
    defendant_args text,
    plaintiff_rebuttal text,
    defendant_counter text,
    verdict text,
    judge_plaintiff_score numeric,
    judge_defendant_score numeric,
    judge_reasoning text,
    unanswered_questions jsonb DEFAULT '[]'::jsonb NOT NULL,
    total_cost_usd numeric DEFAULT 0 NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: usage_rollup_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_rollup_daily (
    day date NOT NULL,
    user_id uuid NOT NULL,
    task_count integer DEFAULT 0 NOT NULL,
    credits_spent integer DEFAULT 0 NOT NULL,
    tokens_used bigint DEFAULT 0 NOT NULL,
    tool_calls integer DEFAULT 0 NOT NULL,
    errors integer DEFAULT 0 NOT NULL,
    model_breakdown jsonb DEFAULT '{}'::jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_events ALTER COLUMN id SET DEFAULT nextval('public.agent_events_id_seq'::regclass);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: auth; Owner: -
--

ALTER TABLE ONLY auth.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: agent_events agent_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_pkey PRIMARY KEY (id);


--
-- Name: agent_settlements agent_settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_settlements
    ADD CONSTRAINT agent_settlements_pkey PRIMARY KEY (task_id);


--
-- Name: agent_tasks agent_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tasks
    ADD CONSTRAINT agent_tasks_pkey PRIMARY KEY (id);


--
-- Name: ai_sessions ai_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_sessions
    ADD CONSTRAINT ai_sessions_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: audit_results audit_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_results
    ADD CONSTRAINT audit_results_pkey PRIMARY KEY (id);


--
-- Name: branches branches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branches
    ADD CONSTRAINT branches_pkey PRIMARY KEY (id);


--
-- Name: checkpoints checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (id);


--
-- Name: claims claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claims
    ADD CONSTRAINT claims_pkey PRIMARY KEY (id);


--
-- Name: contradiction_pairs contradiction_pairs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contradiction_pairs
    ADD CONSTRAINT contradiction_pairs_pkey PRIMARY KEY (id);


--
-- Name: conversations conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (id);


--
-- Name: credit_buckets credit_buckets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_buckets
    ADD CONSTRAINT credit_buckets_pkey PRIMARY KEY (id);


--
-- Name: credit_clawbacks credit_clawbacks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_clawbacks
    ADD CONSTRAINT credit_clawbacks_pkey PRIMARY KEY (id);


--
-- Name: credit_clawbacks credit_clawbacks_ref_type_ref_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_clawbacks
    ADD CONSTRAINT credit_clawbacks_ref_type_ref_id_key UNIQUE (ref_type, ref_id);


--
-- Name: credit_transactions credit_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transactions
    ADD CONSTRAINT credit_transactions_pkey PRIMARY KEY (id);


--
-- Name: evaluation_results evaluation_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evaluation_results
    ADD CONSTRAINT evaluation_results_pkey PRIMARY KEY (id);


--
-- Name: executive_summaries executive_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.executive_summaries
    ADD CONSTRAINT executive_summaries_pkey PRIMARY KEY (id);


--
-- Name: findings findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_pkey PRIMARY KEY (id);


--
-- Name: gap_analyses gap_analyses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gap_analyses
    ADD CONSTRAINT gap_analyses_pkey PRIMARY KEY (id);


--
-- Name: graph_edges graph_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_edges
    ADD CONSTRAINT graph_edges_pkey PRIMARY KEY (id);


--
-- Name: graph_nodes graph_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_nodes
    ADD CONSTRAINT graph_nodes_pkey PRIMARY KEY (id);


--
-- Name: hypotheses hypotheses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypotheses
    ADD CONSTRAINT hypotheses_pkey PRIMARY KEY (id);


--
-- Name: hypothesis_priors hypothesis_priors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypothesis_priors
    ADD CONSTRAINT hypothesis_priors_pkey PRIMARY KEY (id);


--
-- Name: hypothesis_priors hypothesis_priors_task_id_hypothesis_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypothesis_priors
    ADD CONSTRAINT hypothesis_priors_task_id_hypothesis_id_key UNIQUE (task_id, hypothesis_id);


--
-- Name: investigation_outcomes investigation_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investigation_outcomes
    ADD CONSTRAINT investigation_outcomes_pkey PRIMARY KEY (id);


--
-- Name: investigation_outcomes investigation_outcomes_task_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investigation_outcomes
    ADD CONSTRAINT investigation_outcomes_task_id_key UNIQUE (task_id);


--
-- Name: investigations investigations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investigations
    ADD CONSTRAINT investigations_pkey PRIMARY KEY (id);


--
-- Name: learning_events learning_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learning_events
    ADD CONSTRAINT learning_events_pkey PRIMARY KEY (id);


--
-- Name: learning_insights learning_insights_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learning_insights
    ADD CONSTRAINT learning_insights_pkey PRIMARY KEY (id);


--
-- Name: learning_insights learning_insights_user_id_insight_type_insight_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learning_insights
    ADD CONSTRAINT learning_insights_user_id_insight_type_insight_key_key UNIQUE (user_id, insight_type, insight_key);


--
-- Name: loop6_007_applied loop6_007_applied_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.loop6_007_applied
    ADD CONSTRAINT loop6_007_applied_pkey PRIMARY KEY (one_row);


--
-- Name: orchestrator_handoffs orchestrator_handoffs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orchestrator_handoffs
    ADD CONSTRAINT orchestrator_handoffs_pkey PRIMARY KEY (id);


--
-- Name: perspective_syntheses perspective_syntheses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perspective_syntheses
    ADD CONSTRAINT perspective_syntheses_pkey PRIMARY KEY (id);


--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);


--
-- Name: report_generations report_generations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_generations
    ADD CONSTRAINT report_generations_pkey PRIMARY KEY (id);


--
-- Name: research_plans research_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_plans
    ADD CONSTRAINT research_plans_pkey PRIMARY KEY (id);


--
-- Name: research_settlements research_settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_settlements
    ADD CONSTRAINT research_settlements_pkey PRIMARY KEY (task_id);


--
-- Name: research_tasks research_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_tasks
    ADD CONSTRAINT research_tasks_pkey PRIMARY KEY (id);


--
-- Name: skeptic_results skeptic_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skeptic_results
    ADD CONSTRAINT skeptic_results_pkey PRIMARY KEY (id);


--
-- Name: source_scores source_scores_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_scores
    ADD CONSTRAINT source_scores_pkey PRIMARY KEY (id);


--
-- Name: sources sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);


--
-- Name: stripe_dispute_reversals stripe_dispute_reversals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_dispute_reversals
    ADD CONSTRAINT stripe_dispute_reversals_pkey PRIMARY KEY (reversal_key);


--
-- Name: stripe_payment_grants stripe_payment_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_payment_grants
    ADD CONSTRAINT stripe_payment_grants_pkey PRIMARY KEY (payment_intent_id);


--
-- Name: stripe_pending_reversals stripe_pending_reversals_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_pending_reversals
    ADD CONSTRAINT stripe_pending_reversals_event_id_key UNIQUE (event_id);


--
-- Name: stripe_pending_reversals stripe_pending_reversals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_pending_reversals
    ADD CONSTRAINT stripe_pending_reversals_pkey PRIMARY KEY (id);


--
-- Name: stripe_webhook_events stripe_webhook_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_webhook_events
    ADD CONSTRAINT stripe_webhook_events_pkey PRIMARY KEY (event_id);


--
-- Name: system_status system_status_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_status
    ADD CONSTRAINT system_status_pkey PRIMARY KEY (id);


--
-- Name: tribunal_sessions tribunal_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tribunal_sessions
    ADD CONSTRAINT tribunal_sessions_pkey PRIMARY KEY (id);


--
-- Name: usage_rollup_daily usage_rollup_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_rollup_daily
    ADD CONSTRAINT usage_rollup_daily_pkey PRIMARY KEY (day, user_id);


--
-- Name: idx_agent_events_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_events_task_id ON public.agent_events USING btree (task_id, id);


--
-- Name: idx_agent_settlements_completed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_settlements_completed ON public.agent_settlements USING btree (completed_at) WHERE (completed_at IS NULL);


--
-- Name: idx_agent_settlements_ledger_applied_pending_complete; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_settlements_ledger_applied_pending_complete ON public.agent_settlements USING btree (ledger_applied_at) WHERE ((completed_at IS NULL) AND (ledger_applied_at IS NOT NULL));


--
-- Name: idx_agent_tasks_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_tasks_created ON public.agent_tasks USING btree (created_at DESC);


--
-- Name: idx_agent_tasks_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_tasks_state ON public.agent_tasks USING btree (state);


--
-- Name: idx_agent_tasks_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_tasks_user_id ON public.agent_tasks USING btree (user_id);


--
-- Name: idx_ai_sessions_branch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_sessions_branch_id ON public.ai_sessions USING btree (branch_id);


--
-- Name: idx_ai_sessions_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_sessions_task_id ON public.ai_sessions USING btree (task_id);


--
-- Name: idx_audit_results_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_audit_results_task ON public.audit_results USING btree (task_id);


--
-- Name: idx_branches_hypothesis_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branches_hypothesis_id ON public.branches USING btree (hypothesis_id);


--
-- Name: idx_branches_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branches_status ON public.branches USING btree (status);


--
-- Name: idx_branches_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_branches_task_id ON public.branches USING btree (task_id);


--
-- Name: idx_checkpoints_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_checkpoints_task_id ON public.checkpoints USING btree (task_id);


--
-- Name: idx_claims_finding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_finding ON public.claims USING btree (finding_id);


--
-- Name: idx_claims_hypothesis; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_hypothesis ON public.claims USING btree (hypothesis_id);


--
-- Name: idx_claims_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_subject ON public.claims USING btree (task_id, subject);


--
-- Name: idx_claims_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claims_task ON public.claims USING btree (task_id);


--
-- Name: idx_contradictions_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contradictions_task ON public.contradiction_pairs USING btree (task_id);


--
-- Name: idx_credit_clawbacks_user_unsatisfied; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_credit_clawbacks_user_unsatisfied ON public.credit_clawbacks USING btree (user_id) WHERE (satisfied_at IS NULL);


--
-- Name: idx_credit_tx_bucket_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_credit_tx_bucket_id ON public.credit_transactions USING btree (bucket_id);


--
-- Name: idx_evaluation_results_branch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_evaluation_results_branch_id ON public.evaluation_results USING btree (branch_id);


--
-- Name: idx_evaluation_results_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_evaluation_results_task_id ON public.evaluation_results USING btree (task_id);


--
-- Name: idx_executive_summaries_task; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_executive_summaries_task ON public.executive_summaries USING btree (task_id);


--
-- Name: idx_findings_hypothesis_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_findings_hypothesis_id ON public.findings USING btree (hypothesis_id);


--
-- Name: idx_findings_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_findings_task_id ON public.findings USING btree (task_id);


--
-- Name: idx_gap_analyses_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gap_analyses_task ON public.gap_analyses USING btree (task_id);


--
-- Name: idx_graph_edges_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_graph_edges_task ON public.graph_edges USING btree (task_id);


--
-- Name: idx_graph_nodes_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_graph_nodes_task ON public.graph_nodes USING btree (task_id);


--
-- Name: idx_handoffs_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoffs_task ON public.orchestrator_handoffs USING btree (task_id);


--
-- Name: idx_hypotheses_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_parent_id ON public.hypotheses USING btree (parent_id);


--
-- Name: idx_hypotheses_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypotheses_task_id ON public.hypotheses USING btree (task_id);


--
-- Name: idx_hypothesis_priors_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hypothesis_priors_task ON public.hypothesis_priors USING btree (task_id);


--
-- Name: idx_investigation_outcomes_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_investigation_outcomes_task ON public.investigation_outcomes USING btree (task_id);


--
-- Name: idx_investigation_outcomes_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_investigation_outcomes_user ON public.investigation_outcomes USING btree (user_id);


--
-- Name: idx_investigations_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_investigations_user_id ON public.investigations USING btree (user_id);


--
-- Name: idx_learning_events_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_learning_events_task ON public.learning_events USING btree (task_id);


--
-- Name: idx_learning_events_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_learning_events_type ON public.learning_events USING btree (event_type);


--
-- Name: idx_learning_events_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_learning_events_user ON public.learning_events USING btree (user_id);


--
-- Name: idx_learning_insights_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_learning_insights_type ON public.learning_insights USING btree (user_id, insight_type);


--
-- Name: idx_learning_insights_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_learning_insights_user ON public.learning_insights USING btree (user_id);


--
-- Name: idx_perspective_syntheses_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_perspective_syntheses_task ON public.perspective_syntheses USING btree (task_id);


--
-- Name: idx_report_generations_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_report_generations_task_id ON public.report_generations USING btree (task_id);


--
-- Name: idx_research_plans_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_research_plans_task ON public.research_plans USING btree (task_id);


--
-- Name: idx_research_settlements_completed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_research_settlements_completed ON public.research_settlements USING btree (completed_at) WHERE (completed_at IS NULL);


--
-- Name: idx_research_settlements_ledger_applied_pending_complete; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_research_settlements_ledger_applied_pending_complete ON public.research_settlements USING btree (ledger_applied_at) WHERE ((completed_at IS NULL) AND (ledger_applied_at IS NOT NULL));


--
-- Name: idx_research_tasks_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_research_tasks_user_id ON public.research_tasks USING btree (user_id);


--
-- Name: idx_skeptic_results_finding_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_skeptic_results_finding_id ON public.skeptic_results USING btree (finding_id);


--
-- Name: idx_skeptic_results_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_skeptic_results_task_id ON public.skeptic_results USING btree (task_id);


--
-- Name: idx_source_scores_source; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_source_scores_source ON public.source_scores USING btree (source_id);


--
-- Name: idx_source_scores_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_source_scores_task ON public.source_scores USING btree (task_id);


--
-- Name: idx_sources_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sources_task_id ON public.sources USING btree (task_id);


--
-- Name: idx_sources_url_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_sources_url_hash ON public.sources USING btree (task_id, url_hash);


--
-- Name: idx_stripe_dispute_reversals_charge; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_dispute_reversals_charge ON public.stripe_dispute_reversals USING btree (charge_id);


--
-- Name: idx_stripe_dispute_reversals_dispute; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_dispute_reversals_dispute ON public.stripe_dispute_reversals USING btree (dispute_id);


--
-- Name: idx_stripe_payment_grants_charge; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_payment_grants_charge ON public.stripe_payment_grants USING btree (charge_id);


--
-- Name: idx_stripe_payment_grants_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_payment_grants_user ON public.stripe_payment_grants USING btree (user_id);


--
-- Name: idx_stripe_pending_reversals_charge_unapplied; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_pending_reversals_charge_unapplied ON public.stripe_pending_reversals USING btree (charge_id) WHERE (applied_at IS NULL);


--
-- Name: idx_stripe_pending_reversals_pi_unapplied; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_pending_reversals_pi_unapplied ON public.stripe_pending_reversals USING btree (payment_intent_id) WHERE (applied_at IS NULL);


--
-- Name: idx_stripe_webhook_events_processed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_webhook_events_processed_at ON public.stripe_webhook_events USING btree (processed_at);


--
-- Name: idx_stripe_webhook_events_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stripe_webhook_events_status ON public.stripe_webhook_events USING btree (status) WHERE (status = 'pending'::text);


--
-- Name: idx_system_status_frozen_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_system_status_frozen_by ON public.system_status USING btree (frozen_by);


--
-- Name: idx_tribunal_sessions_finding_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tribunal_sessions_finding_id ON public.tribunal_sessions USING btree (finding_id);


--
-- Name: idx_tribunal_sessions_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tribunal_sessions_task_id ON public.tribunal_sessions USING btree (task_id);


--
-- Name: uq_credit_tx_idem; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_credit_tx_idem ON public.credit_transactions USING btree (ref_type, ref_id, type) WHERE ((type = ANY (ARRAY['grant'::text, 'refund'::text, 'expiry'::text])) AND (ref_type IS NOT NULL) AND (ref_id IS NOT NULL));


--
-- Name: agent_events agent_events_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_events
    ADD CONSTRAINT agent_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.agent_tasks(id) ON DELETE CASCADE;


--
-- Name: agent_settlements agent_settlements_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_settlements
    ADD CONSTRAINT agent_settlements_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.agent_tasks(id) ON DELETE RESTRICT;


--
-- Name: ai_sessions ai_sessions_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_sessions
    ADD CONSTRAINT ai_sessions_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: audit_results audit_results_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_results
    ADD CONSTRAINT audit_results_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: branches branches_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branches
    ADD CONSTRAINT branches_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES public.hypotheses(id) ON DELETE CASCADE;


--
-- Name: branches branches_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.branches
    ADD CONSTRAINT branches_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: checkpoints checkpoints_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: claims claims_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claims
    ADD CONSTRAINT claims_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.findings(id) ON DELETE SET NULL;


--
-- Name: claims claims_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claims
    ADD CONSTRAINT claims_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES public.hypotheses(id) ON DELETE SET NULL;


--
-- Name: claims claims_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claims
    ADD CONSTRAINT claims_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: contradiction_pairs contradiction_pairs_claim_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contradiction_pairs
    ADD CONSTRAINT contradiction_pairs_claim_a_id_fkey FOREIGN KEY (claim_a_id) REFERENCES public.claims(id) ON DELETE CASCADE;


--
-- Name: contradiction_pairs contradiction_pairs_claim_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contradiction_pairs
    ADD CONSTRAINT contradiction_pairs_claim_b_id_fkey FOREIGN KEY (claim_b_id) REFERENCES public.claims(id) ON DELETE CASCADE;


--
-- Name: contradiction_pairs contradiction_pairs_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contradiction_pairs
    ADD CONSTRAINT contradiction_pairs_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: evaluation_results evaluation_results_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evaluation_results
    ADD CONSTRAINT evaluation_results_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: executive_summaries executive_summaries_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.executive_summaries
    ADD CONSTRAINT executive_summaries_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: findings findings_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES public.hypotheses(id) ON DELETE CASCADE;


--
-- Name: findings findings_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: gap_analyses gap_analyses_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gap_analyses
    ADD CONSTRAINT gap_analyses_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: graph_edges graph_edges_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_edges
    ADD CONSTRAINT graph_edges_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: graph_nodes graph_nodes_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_nodes
    ADD CONSTRAINT graph_nodes_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: hypotheses hypotheses_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypotheses
    ADD CONSTRAINT hypotheses_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.hypotheses(id) ON DELETE SET NULL;


--
-- Name: hypotheses hypotheses_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypotheses
    ADD CONSTRAINT hypotheses_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: hypothesis_priors hypothesis_priors_hypothesis_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypothesis_priors
    ADD CONSTRAINT hypothesis_priors_hypothesis_id_fkey FOREIGN KEY (hypothesis_id) REFERENCES public.hypotheses(id) ON DELETE CASCADE;


--
-- Name: hypothesis_priors hypothesis_priors_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hypothesis_priors
    ADD CONSTRAINT hypothesis_priors_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: investigation_outcomes investigation_outcomes_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.investigation_outcomes
    ADD CONSTRAINT investigation_outcomes_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: learning_events learning_events_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learning_events
    ADD CONSTRAINT learning_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE SET NULL;


--
-- Name: orchestrator_handoffs orchestrator_handoffs_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orchestrator_handoffs
    ADD CONSTRAINT orchestrator_handoffs_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: perspective_syntheses perspective_syntheses_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.perspective_syntheses
    ADD CONSTRAINT perspective_syntheses_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: report_generations report_generations_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_generations
    ADD CONSTRAINT report_generations_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: research_plans research_plans_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_plans
    ADD CONSTRAINT research_plans_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: research_settlements research_settlements_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_settlements
    ADD CONSTRAINT research_settlements_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE RESTRICT;


--
-- Name: research_tasks research_tasks_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.research_tasks
    ADD CONSTRAINT research_tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: skeptic_results skeptic_results_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skeptic_results
    ADD CONSTRAINT skeptic_results_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.findings(id) ON DELETE CASCADE;


--
-- Name: skeptic_results skeptic_results_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skeptic_results
    ADD CONSTRAINT skeptic_results_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: skeptic_results skeptic_results_tribunal_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skeptic_results
    ADD CONSTRAINT skeptic_results_tribunal_session_id_fkey FOREIGN KEY (tribunal_session_id) REFERENCES public.tribunal_sessions(id) ON DELETE SET NULL;


--
-- Name: source_scores source_scores_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_scores
    ADD CONSTRAINT source_scores_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id) ON DELETE CASCADE;


--
-- Name: source_scores source_scores_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_scores
    ADD CONSTRAINT source_scores_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: sources sources_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: stripe_dispute_reversals stripe_dispute_reversals_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_dispute_reversals
    ADD CONSTRAINT stripe_dispute_reversals_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: stripe_payment_grants stripe_payment_grants_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_payment_grants
    ADD CONSTRAINT stripe_payment_grants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;


--
-- Name: tribunal_sessions tribunal_sessions_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tribunal_sessions
    ADD CONSTRAINT tribunal_sessions_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.findings(id) ON DELETE CASCADE;


--
-- Name: tribunal_sessions tribunal_sessions_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tribunal_sessions
    ADD CONSTRAINT tribunal_sessions_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.research_tasks(id) ON DELETE CASCADE;


--
-- Name: conversations Users can create own conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can create own conversations" ON public.conversations FOR INSERT WITH CHECK ((( SELECT auth.uid() AS uid) = user_id));


--
-- Name: investigations Users can create own investigations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can create own investigations" ON public.investigations FOR INSERT WITH CHECK ((( SELECT auth.uid() AS uid) = user_id));


--
-- Name: conversations Users can delete own conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can delete own conversations" ON public.conversations FOR DELETE USING ((( SELECT auth.uid() AS uid) = user_id));


--
-- Name: conversations Users can read own conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can read own conversations" ON public.conversations FOR SELECT USING ((( SELECT auth.uid() AS uid) = user_id));


--
-- Name: investigations Users can read own investigations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can read own investigations" ON public.investigations FOR SELECT USING ((( SELECT auth.uid() AS uid) = user_id));


--
-- Name: conversations Users can update own conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can update own conversations" ON public.conversations FOR UPDATE USING ((( SELECT auth.uid() AS uid) = user_id));


--
-- Name: audit_log audit_log_admin_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY audit_log_admin_read ON public.audit_log FOR SELECT USING (public.is_admin(( SELECT auth.uid() AS uid)));


--
-- Name: credit_clawbacks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.credit_clawbacks ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_clawbacks credit_clawbacks_owner_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY credit_clawbacks_owner_select ON public.credit_clawbacks FOR SELECT USING ((( SELECT auth.uid() AS uid) = user_id));


--
-- Name: loop6_007_applied; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.loop6_007_applied ENABLE ROW LEVEL SECURITY;

--
-- Name: loop6_008_applied; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.loop6_008_applied ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles profiles_owner_update_safe; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY profiles_owner_update_safe ON public.profiles FOR UPDATE USING ((( SELECT auth.uid() AS uid) = id)) WITH CHECK (((( SELECT auth.uid() AS uid) = id) AND public.check_profile_immutable(id, role, plan, tokens, stripe_customer_id, stripe_subscription_id, subscription_status, subscription_plan, subscription_current_period_end, suspended_at, suspended_reason, admin_notes)));


--
-- Name: stripe_dispute_reversals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.stripe_dispute_reversals ENABLE ROW LEVEL SECURITY;

--
-- Name: stripe_payment_grants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.stripe_payment_grants ENABLE ROW LEVEL SECURITY;

--
-- Name: stripe_pending_reversals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.stripe_pending_reversals ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_rollup_daily usage_rollup_daily_admin_all; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY usage_rollup_daily_admin_all ON public.usage_rollup_daily USING (public.is_admin(( SELECT auth.uid() AS uid)));


--
-- Name: usage_rollup_daily usage_rollup_daily_owner_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY usage_rollup_daily_owner_select ON public.usage_rollup_daily FOR SELECT USING ((( SELECT auth.uid() AS uid) = user_id));


--
-- PostgreSQL database dump complete
--


