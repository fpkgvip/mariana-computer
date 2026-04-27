-- C16_fk_indexes.sql
-- Contract test: B-32 — 7 FK columns must have covering indexes
-- All indexes may not exist in the testdb (some tables are live-only),
-- but the ones for tables present in testdb must exist.
-- Failure mode: sequential scans on FK columns used in RLS + CASCADE DELETE.

-- Test 1: investigations.user_id must be indexed
DO $$
DECLARE
  v_idx text;
BEGIN
  SELECT indexname INTO v_idx
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'investigations'
    AND indexdef ILIKE '%user_id%'
  LIMIT 1;
  IF v_idx IS NULL THEN
    RAISE EXCEPTION 'B-32 FAIL: investigations(user_id) has no index — RLS and JOIN scans will be sequential';
  END IF;
END $$;

-- Test 2: credit_transactions.bucket_id must be indexed
DO $$
DECLARE
  v_idx text;
BEGIN
  SELECT indexname INTO v_idx
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'credit_transactions'
    AND indexdef ILIKE '%bucket_id%'
  LIMIT 1;
  IF v_idx IS NULL THEN
    RAISE EXCEPTION 'B-32 FAIL: credit_transactions(bucket_id) has no index — expire_credits FOR UPDATE scans will be sequential';
  END IF;
END $$;

-- Test 3: system_status.frozen_by must be indexed
DO $$
DECLARE
  v_idx text;
BEGIN
  SELECT indexname INTO v_idx
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'system_status'
    AND indexdef ILIKE '%frozen_by%'
  LIMIT 1;
  IF v_idx IS NULL THEN
    RAISE EXCEPTION 'B-32 FAIL: system_status(frozen_by) has no index — FK cascade scan will be sequential';
  END IF;
END $$;

-- Test 4: If admin_tasks table exists, assigned_to must be indexed
DO $$
DECLARE
  v_idx text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'admin_tasks'
  ) THEN
    RETURN; -- table not in this env, skip
  END IF;
  SELECT indexname INTO v_idx
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'admin_tasks'
    AND indexdef ILIKE '%assigned_to%'
  LIMIT 1;
  IF v_idx IS NULL THEN
    RAISE EXCEPTION 'B-32 FAIL: admin_tasks(assigned_to) has no index';
  END IF;
END $$;

-- Test 5: If admin_tasks table exists, created_by must be indexed
DO $$
DECLARE
  v_idx text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'admin_tasks'
  ) THEN
    RETURN; -- table not in this env, skip
  END IF;
  SELECT indexname INTO v_idx
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'admin_tasks'
    AND indexdef ILIKE '%created_by%'
  LIMIT 1;
  IF v_idx IS NULL THEN
    RAISE EXCEPTION 'B-32 FAIL: admin_tasks(created_by) has no index';
  END IF;
END $$;

-- Test 6: If feature_flags table exists, updated_by must be indexed
DO $$
DECLARE
  v_idx text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'feature_flags'
  ) THEN
    RETURN; -- table not in this env, skip
  END IF;
  SELECT indexname INTO v_idx
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'feature_flags'
    AND indexdef ILIKE '%updated_by%'
  LIMIT 1;
  IF v_idx IS NULL THEN
    RAISE EXCEPTION 'B-32 FAIL: feature_flags(updated_by) has no index';
  END IF;
END $$;

-- Test 7: If messages table exists, investigation_id must be indexed
DO $$
DECLARE
  v_idx text;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'messages'
  ) THEN
    RETURN; -- table not in this env, skip
  END IF;
  SELECT indexname INTO v_idx
  FROM pg_indexes
  WHERE schemaname = 'public'
    AND tablename = 'messages'
    AND indexdef ILIKE '%investigation_id%'
  LIMIT 1;
  IF v_idx IS NULL THEN
    RAISE EXCEPTION 'B-32 FAIL: messages(investigation_id) has no index — RLS subquery scans will be sequential';
  END IF;
END $$;
