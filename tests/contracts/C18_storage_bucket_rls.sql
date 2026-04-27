-- C18_storage_bucket_rls.sql
-- Contract test: B-35 — property-images bucket must not allow unauthenticated listing
-- If the storage schema is not present (local testdb), this test passes vacuously.

-- Test 1: If storage schema exists and objects table has policies, verify no broad
-- anon-listing policy for property-images exists
DO $$
BEGIN
  -- Skip if no storage schema (local testdb environment)
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'storage'
  ) THEN
    RAISE NOTICE 'C18: storage schema not present — skipping (local env, expected)';
    RETURN;
  END IF;

  -- Skip if no objects table in storage (should not happen on live Supabase)
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'storage' AND table_name = 'objects'
  ) THEN
    RAISE NOTICE 'C18: storage.objects not present — skipping';
    RETURN;
  END IF;

  -- Verify: the broad anon policy "Property images are publicly accessible"
  -- should NOT exist (it was dropped by migration 016)
  IF EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'Property images are publicly accessible'
  ) THEN
    RAISE EXCEPTION 'B-35 FAIL: broad anon SELECT policy "Property images are publicly accessible" still exists on storage.objects — unauthenticated file listing is open';
  END IF;

  RAISE NOTICE 'C18: broad anon listing policy absent — OK';
END $$;

-- Test 2: If storage schema exists, verify an authenticated-only policy exists
-- for property-images (added by migration 016)
DO $$
BEGIN
  -- Skip if no storage schema
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'storage'
  ) THEN
    RAISE NOTICE 'C18: storage schema not present — skipping (local env, expected)';
    RETURN;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'property-images authenticated select'
  ) THEN
    RAISE EXCEPTION 'B-35 FAIL: "property-images authenticated select" policy not found on storage.objects — bucket access is not restricted to authenticated users';
  END IF;

  RAISE NOTICE 'C18: authenticated SELECT policy present — OK';
END $$;
