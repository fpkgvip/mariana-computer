-- ============================================================
-- Migration 013: B-32 — 7 FK columns missing covering indexes
-- Loop 6 / P3 DB cluster
-- ============================================================
-- Problem: 7 FK columns lack btree indexes, causing sequential scans
-- on cascade-delete and JOIN operations. The most critical is
-- investigations.user_id (used in every RLS policy evaluation).
--
-- NOTE: apply_migration wraps in transaction; cannot use CONCURRENTLY.
-- Plain CREATE INDEX is used (safe inside transaction, locks briefly).
--
-- Ref: A1-12, loop6_audit/A1_db.md
-- ============================================================

-- 1. investigations.user_id — highest priority: every RLS policy check
CREATE INDEX IF NOT EXISTS idx_investigations_user_id
  ON public.investigations(user_id);

-- 2. credit_transactions.bucket_id — affects expire_credits FOR UPDATE scan
CREATE INDEX IF NOT EXISTS idx_credit_tx_bucket_id
  ON public.credit_transactions(bucket_id);

-- 3. system_status.frozen_by — FK to profiles(id)
CREATE INDEX IF NOT EXISTS idx_system_status_frozen_by
  ON public.system_status(frozen_by);

-- 4. admin_tasks.assigned_to — FK to profiles(id)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'admin_tasks'
  ) THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_admin_tasks_assigned_to ON public.admin_tasks(assigned_to)';
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_admin_tasks_created_by ON public.admin_tasks(created_by)';
  END IF;
END $$;

-- 5. feature_flags.updated_by — FK to profiles(id)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'feature_flags'
  ) THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_feature_flags_updated_by ON public.feature_flags(updated_by)';
  END IF;
END $$;

-- 6. messages.investigation_id — used in RLS subquery for messages table
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'messages'
  ) THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_messages_investigation_id ON public.messages(investigation_id)';
  END IF;
END $$;
