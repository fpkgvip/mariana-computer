-- Revert for migration 019: restore public access to loop6_007_applied + loop6_008_applied.
-- This revert exists for completeness; do not apply in production.

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['loop6_007_applied','loop6_008_applied']
  LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=t) THEN
      EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t);
      EXECUTE format('GRANT ALL ON public.%I TO anon, authenticated', t);
      RAISE NOTICE 'I-03 revert: restored public access on %', t;
    ELSE
      RAISE NOTICE 'I-03 revert: % does not exist, skipping', t;
    END IF;
  END LOOP;
END $$;
