-- ============================================================
-- Revert 016: restore property-images storage bucket to open listing
-- WARNING: this restores the unauthenticated listing vulnerability (B-35)
-- ============================================================

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'storage'
  ) THEN
    RAISE NOTICE 'storage schema not present — nothing to revert';
    RETURN;
  END IF;

  -- Drop the restricted policies added by 016
  IF EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'property-images authenticated select'
  ) THEN
    EXECUTE 'DROP POLICY "property-images authenticated select" ON storage.objects';
  END IF;

  IF EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'property-images authenticated insert'
  ) THEN
    EXECUTE 'DROP POLICY "property-images authenticated insert" ON storage.objects';
  END IF;

  -- Restore the original broad SELECT policy
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'Property images are publicly accessible'
  ) THEN
    EXECUTE $p$
      CREATE POLICY "Property images are publicly accessible"
        ON storage.objects
        FOR SELECT
        USING (bucket_id = 'property-images')
    $p$;
  END IF;

END $$;
