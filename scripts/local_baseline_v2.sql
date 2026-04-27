-- local_baseline_v2.sql
-- Faithful local replica of NestD live state circa 2026-04-27 19:50 HKT.
-- Intent: support RED-verification of Loop 6 contract tests for B-01..
-- through B-46 against a local Postgres without Supabase.
--
-- Build with: scripts/build_local_baseline_v2.sh
--
-- This file represents the *current live* state. Migrations 005+ from
-- frontend/supabase/migrations/ are applied AFTER this file.

-- Roles ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
    CREATE ROLE anon NOLOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
    CREATE ROLE authenticated NOLOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
    CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='supabase_admin') THEN
    CREATE ROLE supabase_admin NOLOGIN NOINHERIT;
  END IF;
END $$;

-- Auth schema stub ----------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
  id uuid PRIMARY KEY,
  email text,
  raw_user_meta_data jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz DEFAULT now()
);

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true),'')::uuid $$;

CREATE OR REPLACE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT COALESCE(current_setting('request.jwt.claim.role', true), 'anon') $$;

-- Public tables (mirrors live schema) ---------------------------------------
CREATE TABLE IF NOT EXISTS public.profiles (
  id uuid PRIMARY KEY,
  email text NOT NULL,
  full_name text,
  tokens integer NOT NULL DEFAULT 500,
  plan text NOT NULL DEFAULT 'flagship',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  role text NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin','banned')),
  stripe_customer_id text,
  stripe_subscription_id text,
  subscription_status text DEFAULT 'none',
  subscription_plan text DEFAULT 'none',
  subscription_current_period_end timestamptz,
  suspended_at timestamptz,
  suspended_reason text,
  admin_notes text
);

CREATE TABLE IF NOT EXISTS public.credit_buckets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  source text NOT NULL CHECK (source IN ('signup_grant','plan_renewal','topup','admin_grant','refund')),
  original_credits integer NOT NULL,
  remaining_credits integer NOT NULL,
  granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz,
  ref_type text,
  ref_id text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS public.credit_transactions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  type text NOT NULL CHECK (type IN ('grant','spend','refund','expiry')),
  credits integer NOT NULL CHECK (credits > 0),
  bucket_id uuid,
  ref_type text,
  ref_id text,
  balance_after integer NOT NULL,
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Loop 5 idempotency index — POST-004b shape as live (NestD) has it.
-- 004 + 004b were applied to live on 2026-04-25. C03 asserts the post-004b
-- shape (type IN ('grant','refund','expiry') AND ref_type/ref_id NOT NULL).
-- baseline_v2 is a faithful live replica, so we use the post-004b definition.
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_tx_idem
  ON public.credit_transactions (ref_type, ref_id, type)
  WHERE type IN ('grant', 'refund', 'expiry')
    AND ref_type IS NOT NULL
    AND ref_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.audit_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.system_status (
  id integer PRIMARY KEY DEFAULT 1,
  frozen boolean NOT NULL DEFAULT false,
  frozen_reason text,
  frozen_by uuid,
  frozen_at timestamptz,
  maintenance_message text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.system_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.investigations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  ticker text DEFAULT '',
  hypothesis text DEFAULT '',
  status text NOT NULL DEFAULT 'PENDING',
  depth text NOT NULL DEFAULT 'deep',
  model text NOT NULL DEFAULT 'fast',
  budget_usd numeric NOT NULL DEFAULT 50.00,
  backend_investigation_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  task_id text,
  topic text,
  duration_hours double precision DEFAULT 2.0,
  output_pdf_path text,
  output_docx_path text,
  conversation_id uuid
);

CREATE TABLE IF NOT EXISTS public.usage_rollup_daily (
  day date NOT NULL,
  user_id uuid NOT NULL,
  task_count integer NOT NULL DEFAULT 0,
  credits_spent integer NOT NULL DEFAULT 0,
  tokens_used bigint NOT NULL DEFAULT 0,
  tool_calls integer NOT NULL DEFAULT 0,
  errors integer NOT NULL DEFAULT 0,
  model_breakdown jsonb DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (day, user_id)
);

CREATE TABLE IF NOT EXISTS public.conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  title text NOT NULL DEFAULT 'New conversation',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Functions (verbatim from live) --------------------------------------------

CREATE OR REPLACE FUNCTION public.is_admin(user_id uuid)
 RETURNS boolean LANGUAGE sql SECURITY DEFINER
 SET search_path TO 'public', 'auth'
AS $$
  SELECT COALESCE((SELECT role = 'admin' FROM public.profiles WHERE id = user_id), false);
$$;

CREATE OR REPLACE FUNCTION public.add_credits(p_user_id uuid, p_credits integer)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  IF p_credits < 0 THEN
    RAISE EXCEPTION 'Credits amount must be non-negative, got %', p_credits;
  END IF;
  UPDATE profiles SET tokens = tokens + p_credits, updated_at = now() WHERE id = p_user_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'User not found: %', p_user_id; END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.deduct_credits(target_user_id uuid, amount integer)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER AS $$
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

CREATE OR REPLACE FUNCTION public.get_user_tokens(target_user_id uuid)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE result INTEGER;
BEGIN
  SELECT tokens INTO result FROM profiles WHERE id = target_user_id;
  RETURN COALESCE(result, 0);
END;
$$;

CREATE OR REPLACE FUNCTION public.check_balance(target_user_id uuid)
 RETURNS integer LANGUAGE sql SECURITY DEFINER
AS $$ SELECT tokens FROM profiles WHERE id = target_user_id; $$;

CREATE OR REPLACE FUNCTION public.get_stripe_customer_id(target_user_id uuid)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE result TEXT;
BEGIN
  SELECT stripe_customer_id INTO result FROM profiles WHERE id = target_user_id;
  RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_profile_by_id(target_user_id uuid, payload jsonb)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  UPDATE profiles
  SET stripe_customer_id = COALESCE(payload->>'stripe_customer_id', stripe_customer_id),
      stripe_subscription_id = COALESCE(payload->>'stripe_subscription_id', stripe_subscription_id),
      subscription_status = COALESCE(payload->>'subscription_status', subscription_status),
      subscription_plan = COALESCE(payload->>'subscription_plan', subscription_plan),
      subscription_current_period_end = CASE
        WHEN payload ? 'subscription_current_period_end'
        THEN (payload->>'subscription_current_period_end')::timestamptz
        ELSE subscription_current_period_end
      END,
      plan = COALESCE(payload->>'plan', plan),
      full_name = COALESCE(payload->>'full_name', full_name),
      updated_at = now()
  WHERE id = target_user_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_profile_by_stripe_customer(target_customer_id text, payload jsonb)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  UPDATE profiles
  SET subscription_status = COALESCE(payload->>'subscription_status', subscription_status),
      subscription_plan = COALESCE(payload->>'subscription_plan', subscription_plan),
      subscription_current_period_end = CASE
        WHEN payload ? 'subscription_current_period_end'
        THEN (payload->>'subscription_current_period_end')::timestamptz
        ELSE subscription_current_period_end
      END,
      updated_at = now()
  WHERE stripe_customer_id = target_customer_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.admin_audit_insert(
  p_actor_id uuid, p_action text,
  p_target_type text DEFAULT NULL, p_target_id text DEFAULT NULL,
  p_before jsonb DEFAULT NULL, p_after jsonb DEFAULT NULL,
  p_metadata jsonb DEFAULT NULL, p_ip text DEFAULT NULL, p_user_agent text DEFAULT NULL)
 RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
 SET search_path TO 'public', 'auth'
AS $$
DECLARE v_id UUID; v_email TEXT;
BEGIN
  IF NOT public.is_admin(p_actor_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  SELECT email INTO v_email FROM public.profiles WHERE id = p_actor_id;
  INSERT INTO public.audit_log (actor_id, actor_email, action, target_type, target_id, before, after, metadata, ip_address, user_agent)
  VALUES (p_actor_id, v_email, p_action, p_target_type, p_target_id, p_before, p_after, p_metadata, p_ip, p_user_agent)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.admin_set_credits(target_user_id uuid, new_credits integer, is_delta boolean DEFAULT false)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
 SET search_path TO ''
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
  IF is_delta THEN v_final := GREATEST(0, v_current + new_credits); ELSE v_final := new_credits; END IF;
  UPDATE public.profiles SET tokens = v_final, updated_at = now() WHERE id = target_user_id;
  PERFORM public.admin_audit_insert(v_caller, 'admin.set_credits', 'user', target_user_id::text,
    jsonb_build_object('tokens', v_current), jsonb_build_object('tokens', v_final),
    jsonb_build_object('mode', CASE WHEN is_delta THEN 'delta' ELSE 'absolute' END, 'amount', new_credits),
    NULL, NULL);
  RETURN v_final;
END;
$$;

CREATE OR REPLACE FUNCTION public.admin_adjust_credits(p_caller uuid, p_target uuid, p_mode text, p_amount integer, p_reason text DEFAULT NULL)
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
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

CREATE OR REPLACE FUNCTION public.admin_count_profiles()
 RETURNS integer LANGUAGE sql SECURITY DEFINER AS $$
  SELECT COUNT(*)::integer FROM profiles
  WHERE (SELECT role FROM profiles WHERE id = auth.uid()) = 'admin';
$$;

CREATE OR REPLACE FUNCTION public.admin_list_profiles()
 RETURNS TABLE(id uuid, email text, full_name text, tokens integer, role text,
   stripe_customer_id text, subscription_plan text, subscription_status text, created_at timestamptz)
 LANGUAGE sql SECURITY DEFINER AS $$
  SELECT p.id, p.email, p.full_name, p.tokens, p.role,
         p.stripe_customer_id, p.subscription_plan, p.subscription_status, p.created_at
  FROM profiles p
  WHERE (SELECT role FROM profiles WHERE id = auth.uid()) = 'admin'
  ORDER BY p.created_at DESC;
$$;

CREATE OR REPLACE FUNCTION public.expire_credits()
 RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE v_count integer := 0; v_b record; v_balance_after integer;
BEGIN
  FOR v_b IN SELECT id, user_id, remaining_credits FROM public.credit_buckets
             WHERE expires_at IS NOT NULL AND expires_at <= clock_timestamp()
             AND remaining_credits > 0 ORDER BY user_id, granted_at FOR UPDATE LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended(v_b.user_id::text, 0));
    UPDATE public.credit_buckets SET remaining_credits = 0 WHERE id = v_b.id;
    SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after FROM public.credit_buckets WHERE user_id = v_b.user_id;
    INSERT INTO public.credit_transactions (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
      VALUES (v_b.user_id, 'expiry', v_b.remaining_credits, v_b.id, 'bucket', v_b.id::text, v_balance_after, jsonb_build_object('source','expiry'));
    v_count := v_count + 1;
  END LOOP;
  RETURN v_count;
END;$$;

CREATE OR REPLACE FUNCTION public.spend_credits(p_user_id uuid, p_credits integer, p_ref_type text, p_ref_id text, p_metadata jsonb DEFAULT '{}'::jsonb)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE v_remaining integer := p_credits; v_total_balance integer; v_bucket record;
        v_take integer; v_balance_after integer; v_spend_rows jsonb := '[]'::jsonb;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN RAISE EXCEPTION 'spend_credits: p_credits must be > 0'; END IF;
  IF p_user_id IS NULL THEN RAISE EXCEPTION 'spend_credits: p_user_id required'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  SELECT COALESCE(SUM(remaining_credits),0) INTO v_total_balance FROM public.credit_buckets
    WHERE user_id=p_user_id AND remaining_credits>0 AND (expires_at IS NULL OR expires_at > clock_timestamp());
  IF v_total_balance < p_credits THEN
    RETURN jsonb_build_object('status','insufficient_balance','balance', v_total_balance, 'requested', p_credits);
  END IF;
  FOR v_bucket IN
    SELECT id, remaining_credits FROM public.credit_buckets
    WHERE user_id=p_user_id AND remaining_credits>0 AND (expires_at IS NULL OR expires_at > clock_timestamp())
    ORDER BY granted_at ASC, id ASC FOR UPDATE
  LOOP
    EXIT WHEN v_remaining <= 0;
    v_take := LEAST(v_bucket.remaining_credits, v_remaining);
    UPDATE public.credit_buckets SET remaining_credits = remaining_credits - v_take WHERE id = v_bucket.id;
    SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after FROM public.credit_buckets WHERE user_id = p_user_id;
    INSERT INTO public.credit_transactions (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
      VALUES (p_user_id, 'spend', v_take, v_bucket.id, p_ref_type, p_ref_id, v_balance_after, p_metadata);
    v_spend_rows := v_spend_rows || jsonb_build_object('bucket_id', v_bucket.id, 'credits', v_take);
    v_remaining := v_remaining - v_take;
  END LOOP;
  IF v_remaining > 0 THEN RAISE EXCEPTION 'spend_credits: failed to debit full amount, % remaining', v_remaining; END IF;
  RETURN jsonb_build_object('status','spent','credits', p_credits, 'balance_after', v_balance_after, 'buckets', v_spend_rows);
END;$$;

CREATE OR REPLACE FUNCTION public.grant_credits(p_user_id uuid, p_credits integer, p_source text,
  p_ref_type text DEFAULT NULL, p_ref_id text DEFAULT NULL, p_expires_at timestamptz DEFAULT NULL)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE v_bucket_id uuid; v_existing_tx uuid; v_balance_after integer;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN RAISE EXCEPTION 'grant_credits: p_credits must be > 0'; END IF;
  IF p_user_id IS NULL THEN RAISE EXCEPTION 'grant_credits: p_user_id required'; END IF;
  IF p_source NOT IN ('signup_grant','plan_renewal','topup','admin_grant','refund') THEN
    RAISE EXCEPTION 'grant_credits: invalid source %', p_source;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  IF p_ref_type IS NOT NULL AND p_ref_id IS NOT NULL THEN
    SELECT id INTO v_existing_tx FROM public.credit_transactions
      WHERE type='grant' AND ref_type=p_ref_type AND ref_id=p_ref_id LIMIT 1;
    IF v_existing_tx IS NOT NULL THEN
      RETURN jsonb_build_object('status','duplicate','transaction_id', v_existing_tx);
    END IF;
  END IF;
  INSERT INTO public.credit_buckets (user_id, source, original_credits, remaining_credits, expires_at, ref_type, ref_id)
    VALUES (p_user_id, p_source, p_credits, p_credits, p_expires_at, p_ref_type, p_ref_id)
    RETURNING id INTO v_bucket_id;
  SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after FROM public.credit_buckets WHERE user_id = p_user_id;
  INSERT INTO public.credit_transactions (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES (p_user_id, 'grant', p_credits, v_bucket_id, p_ref_type, p_ref_id, v_balance_after, jsonb_build_object('source', p_source));
  RETURN jsonb_build_object('status','granted','bucket_id', v_bucket_id, 'credits', p_credits, 'balance_after', v_balance_after);
END;$$;

CREATE OR REPLACE FUNCTION public.refund_credits(p_user_id uuid, p_credits integer, p_ref_type text, p_ref_id text)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $$
DECLARE v_bucket_id uuid; v_existing_tx uuid; v_balance_after integer;
BEGIN
  IF p_credits IS NULL OR p_credits <= 0 THEN RAISE EXCEPTION 'refund_credits: p_credits must be > 0'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  SELECT id INTO v_existing_tx FROM public.credit_transactions
    WHERE type='refund' AND ref_type=p_ref_type AND ref_id=p_ref_id LIMIT 1;
  IF v_existing_tx IS NOT NULL THEN
    RETURN jsonb_build_object('status','duplicate','transaction_id', v_existing_tx);
  END IF;
  INSERT INTO public.credit_buckets (user_id, source, original_credits, remaining_credits, ref_type, ref_id)
    VALUES (p_user_id, 'refund', p_credits, p_credits, p_ref_type, p_ref_id) RETURNING id INTO v_bucket_id;
  SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after FROM public.credit_buckets WHERE user_id = p_user_id;
  INSERT INTO public.credit_transactions (user_id, type, credits, bucket_id, ref_type, ref_id, balance_after, metadata)
    VALUES (p_user_id, 'refund', p_credits, v_bucket_id, p_ref_type, p_ref_id, v_balance_after, jsonb_build_object('source','refund'));
  RETURN jsonb_build_object('status','refunded','bucket_id', v_bucket_id, 'balance_after', v_balance_after);
END;$$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
 RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  INSERT INTO public.profiles (id, email, full_name)
  VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name');
  RETURN NEW;
END;
$$;

-- EXECUTE grants (faithful to live: most are PUBLIC=EXECUTE) ----------------
GRANT EXECUTE ON FUNCTION public.add_credits(uuid, integer)                    TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.deduct_credits(uuid, integer)                 TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_user_tokens(uuid)                         TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.check_balance(uuid)                           TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_stripe_customer_id(uuid)                  TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.update_profile_by_id(uuid, jsonb)             TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.update_profile_by_stripe_customer(text, jsonb)TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.admin_count_profiles()                        TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.admin_list_profiles()                         TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text) TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean)     TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.admin_adjust_credits(uuid, uuid, text, integer, text) TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.expire_credits()                              TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.spend_credits(uuid, integer, text, text, jsonb) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.grant_credits(uuid, integer, text, text, text, timestamptz) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.refund_credits(uuid, integer, text, text)     TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.handle_new_user()                             TO PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.is_admin(uuid)                                TO PUBLIC, anon, authenticated, service_role;
