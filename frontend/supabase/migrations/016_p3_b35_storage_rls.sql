-- ============================================================
-- Migration 016: B-35 — Restrict property-images storage bucket listing
-- Loop 6 / P3 DB cluster
-- ============================================================
-- Problem: the 'property-images' storage bucket has a broad SELECT policy
-- on storage.objects that allows unauthenticated file listing. Any anon
-- client can enumerate all files via GET /storage/v1/object/list/property-images.
--
-- Fix: add RLS policies on storage.buckets and storage.objects requiring
-- the 'authenticated' role for SELECT / list operations on this bucket.
-- This restricts enumeration to logged-in users only.
--
-- The storage schema is managed by Supabase infrastructure. We add policies
-- on storage.objects (the standard approach for Supabase storage RLS).
--
-- Ref: A1-15, loop6_audit/A1_db.md
-- ============================================================

-- Guard: only execute if storage schema exists (live Supabase; skipped locally)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.schemata WHERE schema_name = 'storage'
  ) THEN
    RAISE NOTICE 'storage schema not present — skipping B-35 storage RLS (local env)';
    RETURN;
  END IF;

  -- Drop the broad existing SELECT policy that allows anon listing
  -- The advisor identifies it as "Property images are publicly accessible"
  IF EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'Property images are publicly accessible'
  ) THEN
    EXECUTE 'DROP POLICY "Property images are publicly accessible" ON storage.objects';
    RAISE NOTICE 'B-35: dropped broad anon listing policy on storage.objects';
  END IF;

  -- Drop any other broad policies on property-images if they exist
  IF EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'property-images public select'
  ) THEN
    EXECUTE 'DROP POLICY "property-images public select" ON storage.objects';
  END IF;

  -- Add authenticated-only SELECT policy for property-images bucket
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'property-images authenticated select'
  ) THEN
    EXECUTE $p$
      CREATE POLICY "property-images authenticated select"
        ON storage.objects
        FOR SELECT
        USING (
          bucket_id = 'property-images'
          AND (SELECT auth.role()) = 'authenticated'
        )
    $p$;
    RAISE NOTICE 'B-35: created authenticated-only SELECT policy for property-images';
  END IF;

  -- Add authenticated-only INSERT policy (restrict uploads too)
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'property-images authenticated insert'
  ) THEN
    EXECUTE $p$
      CREATE POLICY "property-images authenticated insert"
        ON storage.objects
        FOR INSERT
        WITH CHECK (
          bucket_id = 'property-images'
          AND (SELECT auth.role()) = 'authenticated'
        )
    $p$;
    RAISE NOTICE 'B-35: created authenticated-only INSERT policy for property-images';
  END IF;

  -- Ensure storage.buckets RLS is enabled (already true in Supabase, but idempotent)
  -- Note: cannot ALTER TABLE storage.buckets in restricted environments;
  -- Supabase manages this. We rely on storage.objects policies only.

END $$;
