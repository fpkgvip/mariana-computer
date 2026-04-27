-- =============================================================================
-- local_baseline.sql
-- Faithful reproduction of NestD project schema captured 2026-04-27.
-- Sources:
--   loop5_research/live_columns.json
--   loop5_research/live_indexes.json
--   loop5_research/live_policies.json
--   loop5_research/live_all_functions.json
--   loop5_research/live_admin_set_credits.json
--   loop5_research/live_audit_expire.json
--   loop5_research/live_credit_rpcs.json
--
-- This is the input to RED-verify. Apply it then run 004 + 004b on top.
-- =============================================================================

-- Extensions Supabase ships with by default
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- -----------------------------------------------------------------------------
-- Supabase auth shim
-- -----------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
  id uuid PRIMARY KEY,
  email text,
  raw_user_meta_data jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz DEFAULT now()
);

DO $$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE authenticated NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE service_role NOLOGIN BYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
  LANGUAGE sql STABLE
AS $$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
  LANGUAGE sql STABLE
AS $$ SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.role', true), ''), 'anon') $$;

GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
GRANT SELECT ON auth.users TO authenticated, service_role;

-- -----------------------------------------------------------------------------
-- profiles
-- -----------------------------------------------------------------------------

CREATE TABLE public.profiles (
  id uuid PRIMARY KEY,
  email text NOT NULL,
  full_name text,
  tokens integer NOT NULL DEFAULT 500,
  plan text NOT NULL DEFAULT 'flagship',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  role text NOT NULL DEFAULT 'user',
  stripe_customer_id text,
  stripe_subscription_id text,
  subscription_status text DEFAULT 'none',
  subscription_plan text DEFAULT 'none',
  subscription_current_period_end timestamptz,
  suspended_at timestamptz,
  suspended_reason text,
  admin_notes text,
  CONSTRAINT profiles_role_check CHECK (role IN ('user','admin','banned'))
);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own profile" ON public.profiles
  FOR SELECT USING (auth.uid() = id);

-- The two conflicting UPDATE policies (R1) — exactly as in live.
CREATE POLICY "Users can update own profile" ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    (auth.uid() = id)
    AND (NOT (role IS DISTINCT FROM (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (tokens IS DISTINCT FROM (SELECT p.tokens FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (plan IS DISTINCT FROM (SELECT p.plan FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (stripe_customer_id IS DISTINCT FROM (SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (stripe_subscription_id IS DISTINCT FROM (SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (subscription_status IS DISTINCT FROM (SELECT p.subscription_status FROM public.profiles p WHERE p.id = auth.uid())))
    AND (NOT (subscription_plan IS DISTINCT FROM (SELECT p.subscription_plan FROM public.profiles p WHERE p.id = auth.uid())))
  );

CREATE POLICY profiles_owner_update_safe ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    (auth.uid() = id)
    AND (role = (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid()))
    AND (plan = (SELECT p.plan FROM public.profiles p WHERE p.id = auth.uid()))
    AND (tokens = (SELECT p.tokens FROM public.profiles p WHERE p.id = auth.uid()))
    AND (COALESCE(stripe_customer_id, '') = COALESCE((SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = auth.uid()), ''))
    AND (COALESCE(stripe_subscription_id, '') = COALESCE((SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid()), ''))
    AND (COALESCE(suspended_at::text, '') = COALESCE((SELECT p.suspended_at::text FROM public.profiles p WHERE p.id = auth.uid()), ''))
  );

GRANT SELECT, UPDATE ON public.profiles TO authenticated;
GRANT ALL ON public.profiles TO service_role;

-- -----------------------------------------------------------------------------
-- credit_buckets
-- -----------------------------------------------------------------------------

CREATE TABLE public.credit_buckets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id),
  source text NOT NULL,
  original_credits integer NOT NULL,
  remaining_credits integer NOT NULL,
  granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz,
  ref_type text,
  ref_id text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT credit_buckets_source_check CHECK (
    source IN ('signup_grant','plan_renewal','topup','admin_grant','refund')
  )
);

CREATE INDEX idx_credit_buckets_expiry ON public.credit_buckets (expires_at)
  WHERE remaining_credits > 0 AND expires_at IS NOT NULL;
CREATE INDEX idx_credit_buckets_user_fifo ON public.credit_buckets (user_id, granted_at)
  WHERE remaining_credits > 0;

ALTER TABLE public.credit_buckets ENABLE ROW LEVEL SECURITY;
CREATE POLICY credit_buckets_owner_select ON public.credit_buckets
  FOR SELECT USING (auth.uid() = user_id);

GRANT SELECT ON public.credit_buckets TO authenticated;
GRANT ALL ON public.credit_buckets TO service_role;

-- -----------------------------------------------------------------------------
-- credit_transactions
-- -----------------------------------------------------------------------------

CREATE TABLE public.credit_transactions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id),
  type text NOT NULL,
  credits integer NOT NULL,
  bucket_id uuid REFERENCES public.credit_buckets(id),
  ref_type text,
  ref_id text,
  balance_after integer NOT NULL,
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT credit_transactions_type_check CHECK (
    type IN ('grant','spend','refund','expiry')
  )
);

CREATE INDEX idx_credit_tx_ref ON public.credit_transactions (ref_type, ref_id);
CREATE INDEX idx_credit_tx_user_time ON public.credit_transactions (user_id, created_at DESC);

-- The narrow legacy unique index (R2 baseline)
CREATE UNIQUE INDEX uq_credit_tx_grant_ref
  ON public.credit_transactions (ref_type, ref_id)
  WHERE type = 'grant' AND ref_type IS NOT NULL AND ref_id IS NOT NULL;

ALTER TABLE public.credit_transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY credit_tx_owner_select ON public.credit_transactions
  FOR SELECT USING (auth.uid() = user_id);

GRANT SELECT ON public.credit_transactions TO authenticated;
GRANT ALL ON public.credit_transactions TO service_role;

-- -----------------------------------------------------------------------------
-- audit_log
-- -----------------------------------------------------------------------------

CREATE TABLE public.audit_log (
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

CREATE INDEX audit_log_action_idx ON public.audit_log (action);
CREATE INDEX audit_log_actor_idx ON public.audit_log (actor_id);
CREATE INDEX audit_log_created_at_idx ON public.audit_log (created_at DESC);
CREATE INDEX audit_log_target_idx ON public.audit_log (target_type, target_id);

ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_log_admin_read ON public.audit_log
  FOR SELECT USING (EXISTS (
    SELECT 1 FROM public.profiles p
    WHERE p.id = auth.uid() AND p.role = 'admin'
  ));

GRANT SELECT ON public.audit_log TO authenticated;
GRANT ALL ON public.audit_log TO service_role;

-- -----------------------------------------------------------------------------
-- system_status
-- -----------------------------------------------------------------------------

CREATE TABLE public.system_status (
  id integer PRIMARY KEY DEFAULT 1,
  frozen boolean NOT NULL DEFAULT false,
  frozen_reason text,
  frozen_by uuid,
  frozen_at timestamptz,
  maintenance_message text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.system_status (id, frozen) VALUES (1, false) ON CONFLICT DO NOTHING;

ALTER TABLE public.system_status ENABLE ROW LEVEL SECURITY;
CREATE POLICY system_status_read ON public.system_status FOR SELECT USING (true);

-- -----------------------------------------------------------------------------
-- RPCs (subset needed for tests; verbatim from live)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.is_admin(user_id uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path TO 'public', 'auth'
AS $$
  SELECT COALESCE(
    (SELECT role = 'admin' FROM public.profiles WHERE id = user_id),
    false
  );
$$;

CREATE OR REPLACE FUNCTION public.admin_audit_insert(
  p_actor_id uuid, p_action text,
  p_target_type text DEFAULT NULL,
  p_target_id text DEFAULT NULL,
  p_before jsonb DEFAULT NULL,
  p_after jsonb DEFAULT NULL,
  p_metadata jsonb DEFAULT NULL,
  p_ip text DEFAULT NULL,
  p_user_agent text DEFAULT NULL
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public', 'auth'
AS $$
DECLARE v_id UUID; v_email TEXT;
BEGIN
  IF NOT public.is_admin(p_actor_id) THEN
    RAISE EXCEPTION 'not_admin';
  END IF;
  SELECT email INTO v_email FROM public.profiles WHERE id = p_actor_id;
  INSERT INTO public.audit_log (actor_id, actor_email, action, target_type, target_id, before, after, metadata, ip_address, user_agent)
  VALUES (p_actor_id, v_email, p_action, p_target_type, p_target_id, p_before, p_after, p_metadata, p_ip, p_user_agent)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$$;

-- admin_set_credits — LIVE BASELINE (the one R5 fixes).
CREATE OR REPLACE FUNCTION public.admin_set_credits(
  target_user_id uuid, new_credits integer, is_delta boolean DEFAULT false
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  caller_role text;
  current_tokens integer;
  final_balance integer;
BEGIN
  SELECT role INTO caller_role FROM profiles WHERE id = auth.uid();
  IF caller_role IS DISTINCT FROM 'admin' THEN
    RAISE EXCEPTION 'Admin access required';
  END IF;

  IF is_delta THEN
    SELECT tokens INTO current_tokens FROM profiles WHERE id = target_user_id;
    IF current_tokens IS NULL THEN
      RAISE EXCEPTION 'User not found';
    END IF;
    final_balance := GREATEST(0, current_tokens + new_credits);
  ELSE
    final_balance := new_credits;
  END IF;

  UPDATE profiles SET tokens = final_balance, updated_at = now()
  WHERE id = target_user_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'User not found';
  END IF;

  RETURN final_balance;
END;
$$;

GRANT EXECUTE ON FUNCTION public.admin_set_credits(uuid, integer, boolean) TO authenticated;
GRANT EXECUTE ON FUNCTION public.admin_audit_insert(uuid, text, text, text, jsonb, jsonb, jsonb, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.is_admin(uuid) TO authenticated, service_role;

-- Marker so we know this seeded.
COMMENT ON SCHEMA public IS 'NestD baseline replica @ 2026-04-27';

SELECT 'baseline ready' AS status;
