-- Migration 019: I-03 lock down loop6_007_applied + loop6_008_applied marker tables.
-- I-03: Both tables had RLS disabled and granted INSERT/SELECT/UPDATE/DELETE/TRUNCATE
-- to anon and authenticated roles, making migration metadata publicly writable from
-- any browser holding the anon key.
-- Fix: enable RLS (no permissive policies — service_role bypasses RLS by default)
-- and revoke all grants from anon/authenticated, matching the posture used by
-- stripe_payment_grants / stripe_dispute_reversals in migration 017.

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['loop6_007_applied','loop6_008_applied']
  LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=t) THEN
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon, authenticated', t);
      EXECUTE format('GRANT ALL ON public.%I TO service_role', t);
      RAISE NOTICE 'I-03: locked down %', t;
    ELSE
      RAISE NOTICE 'I-03: % does not exist, skipping', t;
    END IF;
  END LOOP;
END $$;
