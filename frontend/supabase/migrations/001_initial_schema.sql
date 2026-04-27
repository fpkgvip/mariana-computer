-- ============================================================
-- 001_initial_schema.sql — NestD initial schema
-- ============================================================
-- DRIFT NOTICE (B-36 / A1-16, 2026-04-28):
-- This file contains initial DDL that has been superseded by later migrations:
--
--   1. "Users can update own profile" policy (line ~60): superseded by
--      004_loop5_idempotency_and_rls.sql which DROPs this weak policy and
--      replaces it with profiles_owner_update_safe (strict WITH CHECK).
--
--   2. investigations.ticker / investigations.hypothesis are defined NOT NULL
--      here; migration 20260416092124 (make_ticker_hypothesis_nullable) made
--      them NULLABLE. The live schema has nullable columns.
--
-- scripts/build_local_baseline_v2.sh now drops the stale policy after
-- applying this file (see B-36 fix). Do not remove the DROP there.
-- ============================================================

-- Profiles table (extends Supabase auth.users)
CREATE TABLE IF NOT EXISTS public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  full_name TEXT,
  tokens INTEGER DEFAULT 500 NOT NULL,
  plan TEXT DEFAULT 'flagship' NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

-- Investigations table (mirrors backend but for frontend state)
CREATE TABLE IF NOT EXISTS public.investigations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES public.profiles(id) ON DELETE CASCADE NOT NULL,
  ticker TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  status TEXT DEFAULT 'PENDING' NOT NULL,
  depth TEXT DEFAULT 'deep' NOT NULL,
  model TEXT DEFAULT 'fast' NOT NULL,
  budget_usd NUMERIC(10,2) DEFAULT 50.00 NOT NULL,
  backend_investigation_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

-- Chat messages
CREATE TABLE IF NOT EXISTS public.messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  investigation_id UUID REFERENCES public.investigations(id) ON DELETE CASCADE NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content TEXT NOT NULL,
  type TEXT DEFAULT 'text' CHECK (type IN ('text', 'code', 'status')),
  created_at TIMESTAMPTZ DEFAULT now() NOT NULL
);

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO public.profiles (id, email, full_name)
  VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- RLS policies
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own profile" ON public.profiles
  FOR SELECT USING (auth.uid() = id);

CREATE POLICY "Users can update own profile" ON public.profiles
  FOR UPDATE USING (auth.uid() = id);

CREATE POLICY "Users can read own investigations" ON public.investigations
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users can create own investigations" ON public.investigations
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can read own messages" ON public.messages
  FOR SELECT USING (
    investigation_id IN (
      SELECT id FROM public.investigations WHERE user_id = auth.uid()
    )
  );

CREATE POLICY "Users can create own messages" ON public.messages
  FOR INSERT WITH CHECK (
    investigation_id IN (
      SELECT id FROM public.investigations WHERE user_id = auth.uid()
    )
  );
